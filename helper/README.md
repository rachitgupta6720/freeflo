# freeflo-helper

A small native helper that owns the two things freeflo cannot afford to get
stuck in: the **microphone** (AVAudioEngine) and **transcription** (the macOS
26+ on-device `SpeechAnalyzer` / `SpeechTranscriber`). The Python app spawns it
as a child process and talks JSON lines over stdin/stdout.

Why a separate process: a thread blocked inside CoreAudio cannot be killed, a
process can. If the helper ever hangs or crashes the app sends SIGKILL and
starts another one; the user never sees "restart freeflo". The helper also
polices itself (see [Never stuck](#never-stuck)).

Nothing is bundled: the speech models belong to macOS, are shared with the
system, and are downloaded on demand by the OS. Audio and text never leave the
machine and nothing is written to disk.

- Requires macOS 26 or later on Apple silicon.
- About 550 KB, ~20 MB resident while idle with a model warm (~28 MB with
  two languages).

## Build

```sh
scripts/build_helper.sh            # -> build/freeflo-helper (optimized, arm64, ad-hoc signed)
scripts/build_helper.sh --debug    # unoptimized, with debug info
python3 helper/tests/smoke_test.py # protocol test, no microphone needed
python3 helper/tests/blend_test.py # two-language blending against fixtures, no audio
python3 helper/tests/blend_test.py --ref   # ...and the Swift port against the reference, number by number
```

Only the Xcode Command Line Tools are needed (`swiftc` + `clang`, macOS 26 SDK
or newer); no Xcode, no SwiftPM. The script compiles the Objective-C exception
bridge, compiles and links the Swift sources, embeds `helper/Info.plist` in the
binary, signs it, checks the architecture, runs `--version`, prints the path
and size, and exits non-zero if any step fails.

| Variable | Effect |
| --- | --- |
| `CODESIGN_IDENTITY` | Sign with this identity instead of ad-hoc, with the hardened runtime and `helper/entitlements.plist` (microphone only). |
| `FREEFLO_HELPER_OUT_DIR` | Output directory (default `build/`). |

Layout:

```
helper/
  Info.plist            embedded in the binary (NSMicrophoneUsageDescription)
  entitlements.plist    hardened-runtime entitlements for Developer ID signing
  Sources/
    main.swift          entry point: serve / transcribe-file / blend / diagnose / --version
    Coordinator.swift   command handling and the session state machine
    Models.swift        locales, model groups, asset install, warm analyzer pipelines
    Blend.swift         two languages: picking each sentence from the better recogniser
    Capture.swift       microphone (AVAudioEngine), recovery, test stand-ins
    AudioFeeder.swift   format conversion and the level meter
    Support.swift       JSON output, logging, timeouts
    ObjC/FFObjC.{h,m}   turns NSException into NSError (Swift cannot catch them)
  tests/smoke_test.py   drives the protocol end to end
  tests/blend_test.py   runs tests/fixtures/blend_cases.json through `blend`
  tests/blend_ref.py    reference implementation of the blend (Python)
```

## Command line

```
freeflo-helper serve
freeflo-helper transcribe-file <path> [--locale en-US] [--languages en-IN,hi-IN]
                                      [--context word,word] [--explain]
freeflo-helper blend < two-transcripts.json
freeflo-helper diagnose                  # one JSON object: OS, permissions, locales, groups, input device
freeflo-helper --version                 # "freeflo-helper 1.2.0"
```

`transcribe-file` prints one JSON line and needs no permission:

```json
{"text":"...","audio_sec":11.6,"elapsed_ms":230.7,"locale":"en-US"}
```

On failure it prints `{"error":"<code>","message":"..."}`, repeats the reason
as the last line of stderr, and exits 1. Without `--locale` it uses the
system's locale. With `--languages a,b` both recognisers run and the output
adds `languages`, `used`, `dominant` and `sources` (see
[Two languages](#two-languages)); `--explain` adds the per-sentence
`decisions`, each recogniser's own text (`primary_text`, `secondary_text`)
and its finalised segments with word confidences and times
(`primary_segments`, `secondary_segments`: exactly what `blend` reads, so a
clip can be replayed or turned into a test fixture).

`blend` reads `{"primary":[segments],"secondary":[segments]}` on stdin, where a
segment is `{"text","start","end","words":[[text, confidence, start, end],...]}`
(confidence -1 = none, times -1 = no time range), and prints
`{"text","sources","decisions","primary_chars","secondary_chars"}`. An optional
`"secondary_locale"` says which recogniser the secondary was (the Hindi word
list is used when it is missing or a locale of the India model). Each decision
is `{"start","end","words","secondary_words","primary","secondary","midcap",
"agree","hindi","rule","used"}` with `rule` one of `keep`, `same` (primary
kept) or `floor`, `margin`, `lexicon` (handed to the secondary). Bad input
prints `{"error":"bad_input"}` and exits 1. It needs no audio and no model:
it is how the blending rules are tested.

## Protocol (`serve`)

One JSON object per line, UTF-8, in both directions. stdout carries only
protocol events and is flushed after every line; logs go to stderr. Unknown
fields in a command are ignored, and the app should likewise ignore event
fields it does not know (the ones marked *extra* below are additions to the
base protocol).

### Commands (stdin)

| Command | Answer |
| --- | --- |
| `{"cmd":"ping","seq":N}` | `pong` with the same `seq` |
| `{"cmd":"status"}` | `status` |
| `{"cmd":"prewarm","locale":"en-IN"}` | `prewarmed`, or `error` (`locale_unsupported`, `locale_not_installed`) |
| `{"cmd":"prewarm","locale":"en-IN","languages":["en-IN","hi-IN"]}` | same, for two languages |
| `{"cmd":"start","id":N,"locale":"en-IN","context":["Kiwi","UPI"],"partials":true}` | `started`, then `level`/`partial`, ending in exactly one of `final`, `cancelled`, `error` |
| `{"cmd":"start","id":N,"locale":"en-IN","languages":["en-IN","hi-IN"],...}` | same, listening in both languages |
| `{"cmd":"stop","id":N}` | `final` (or `error`) for that session |
| `{"cmd":"cancel","id":N}` | `cancelled` |
| `{"cmd":"install","locale":"hi-IN"}` | `install_progress`..., then `installed`, or `error` (`locale_unsupported`, `network`, `no_space`, `cancelled`, `install_failed`) |
| `{"cmd":"request_permissions"}` | `status` (see [Permissions](#permissions)) |
| `{"cmd":"quit"}` | releases the mic and exits 0 |

Optional fields (*extra*):

- `start`/`prewarm`: `"partials"` (default `true`); `"fast_partials": true`
  refreshes partial text about every second instead of every ~4 s, at the cost
  of slightly different final text (see [Performance](#performance)).
- `stop`: `"tail_ms": 0-1000` keeps recording that long after the command.
  The default (0) already waits for the audio buffer in flight, so speech up to
  the moment of the command is always included.
- `locale` accepts BCP-47 (`en-IN`), underscores (`en_IN`), a bare language
  (`en`, `hi`) or `auto`/missing for the system's locale. A bare language
  prefers an installed locale in the system's region.
- `start`/`prewarm`: `"languages"`, a list of locales (same spellings as
  `locale`), most used first; it wins over `locale` (send `locale` =
  `languages[0]` for older helpers). The helper walks the list in order,
  leaves out (and reports as `missing`) the ones that are unsupported or not
  installed, silently skips one that uses the same model as an earlier one
  (hi-IN and mul-IN, see [Two languages](#two-languages)), and keeps at most
  `status.max_languages` (2). If the India model ends up first, the other
  language becomes the primary. It is an error only when none is usable (the
  first error, e.g. `locale_unsupported`). With one usable language the
  dictation is an ordinary one.

### Events (stdout)

```
{"event":"ready","version":"1.2.0","os":"27.2","pid":123}             once, at startup
{"event":"pong","seq":N}
{"event":"status","mic":"granted|denied|undetermined","speech":"not_required",
 "supported":["en-IN",...],"installed":["en-IN",...],
 "default_locale":"en-IN","input_device":"MacBook Air Microphone",
 "groups":[["bn-IN",...,"hi-IN",...,"mul-IN",...]],"max_languages":2}
{"event":"prewarmed","locale":"en-IN","resolved":"en-IN"}
{"event":"started","id":N,"locale":"en-IN"}                           the mic is live
{"event":"level","id":N,"rms":0.0-1.0}                                20 per second
{"event":"partial","id":N,"text":"..."}                               full running text, not a delta
{"event":"final","id":N,"text":"...","audio_sec":F,"elapsed_ms":F}
{"event":"cancelled","id":N}
{"event":"install_progress","locale":"hi-IN","fraction":0.0-1.0}
{"event":"installed","locale":"hi-IN","resolved":"hi-IN","already":false,"installed":["en-IN","hi-IN",...]}
{"event":"error","id":N|null,"code":"...","message":"..."}
```

Two-language and download *extras* (helper 1.1):

| Event | Extra fields |
| --- | --- |
| `status` | `groups`: supported locales that share one speech model (only groups of 2+, each sorted; choose one per dictation). `max_languages`: how many languages one dictation can use (2). |
| `prewarmed` | `resolved` is always a plain locale string (the primary). `languages` (only when two are in use): both, primary first. `missing` (only when non-empty): requested languages left out. |
| `started` | `languages` (only when two are in use), `missing` (only when non-empty). `locale` is the primary. |
| `final` | When two languages were in use: `languages` (both, primary first), `used` (those whose words are in the text, primary first), `dominant` (the one most of the text came from; tie -> primary). |
| `installed` | `already`: true when nothing had to be downloaded (no `install_progress` was sent). `installed`: the fresh list of installed locales, so the client need not wait for a `status`. |
| `install_progress` | The first one (fraction 0) is sent at once; after that whenever the fraction moves by 0.5 %, and at least every 2 s with the same value (a heartbeat). |

- `elapsed_ms` is measured from the moment the `stop` line was read to the
  moment `final` is written.
- `locale` in `prewarmed`/`installed`/`install_progress` echoes what the app
  sent; `resolved` (*extra*) is the locale actually used. `started.locale`
  (*extra*) is the resolved one.
- `status.default_locale` (*extra*): best supported locale for the system's
  settings, or null. `status.input_device` (*extra*): name of the default input
  device, null if there is none.
- `level.rms` is a dB-scaled meter: 0 is about -55 dBFS (room noise), 1 is
  about -10 dBFS. Ordinary speech sits around 0.4-0.8.
- `final` may carry two *extra* hints, present only when true:
  `"silent":true` (audio was captured but it was pure digital silence: a muted
  input, or a MacBook's built-in microphone with the lid closed) and
  `"mic_lost":true` (see below).
- Errors that answer `prewarm`/`install` have `"id":null` plus two *extras*:
  `"locale"` (as sent) and `"cmd"` (`"prewarm"` or `"install"`), so the client
  knows which request failed. `bad_command` errors carry `"cmd"` too.

Error codes: `mic_denied`, `no_input_device`, `locale_unsupported`,
`locale_not_installed`, `engine_failed`, `bad_command`, `internal`; for
`install` also `network` (no connection, a timeout, or no progress for 180 s),
`no_space` (not enough disk space), `cancelled` and `install_failed`
(anything else).
(`speech_denied` is reserved by the protocol but never sent, because Speech
Recognition authorisation is not required.)

### Guarantees

- **Every session gets exactly one terminal event** (`final`, `cancelled` or
  `error`), and nothing for that id after it. `level`/`partial` only appear
  between `started` and the terminal event.
- **The mic is released before `final`/`cancelled` is sent.** Verified through
  CoreAudio (`kAudioDevicePropertyDeviceIsRunningSomewhere` goes back to 0),
  which is what drives the orange indicator. The one exception is a CoreAudio
  call that never returns: see [Never stuck](#never-stuck).
- **`start` while a session is active cancels the old one** (`cancelled` for
  the old id, then `started` for the new one).
- **`start` right after `stop` is fine.** The earlier session keeps finalising
  and still delivers its `final`; the two overlap.
- **Unknown ids are ignored**, as are repeated `stop`/`cancel` for a session.
- **`stop` before the mic is live** yields `final` with empty text. If the mic
  was not yet being opened (for example the permission prompt is still up)
  there is no `started` first.
- **`stop` with no speech** yields `final` with `"text":""`.
- **`cancel` after `stop`** turns the pending `final` into `cancelled`.
- **stdin EOF** (the parent died) or SIGTERM/SIGINT: release the mic, exit 0.
- Commands are handled in order and never block one another: `ping` and
  `cancel` are answered while a dictation is starting or finalising.

### Never stuck

Every wait inside the helper has a deadline, and every CoreAudio call runs on
one dedicated queue with a watchdog:

| Situation | What happens |
| --- | --- |
| Opening the mic does not return within 4 s | `error` `engine_failed` for the session, then the helper **exits with code 75** so the app respawns it |
| Closing the mic does not return | the transcript is finalised and `final` sent after a 1 s grace; at 4 s an `error` (`id:null`, `engine_failed`) is sent and the helper exits 75 |
| The speech model will not load (8 s) or attach (5 s) | `error` `internal` for the session |
| Finalisation does not finish within 4 s | `final` with the text recognised so far |
| Input device changes mid-dictation (unplugged, default switched, sample rate changed) | capture is rebuilt on the new device (about 0.2 s); the session and its transcript continue |
| Input device delivers no audio for 2 s | same rebuild |
| The device cannot be reopened (5 attempts over ~1.6 s) | the session is ended by the helper: `final` with `"mic_lost":true` and the text so far, or `error` (`no_input_device`/`engine_failed`) if nothing was said |
| The parent stops reading stdout | `level` events are dropped; nothing blocks |

Timeouts the app should use: longer than 4 s for `start` -> `started` (the
helper answers or exits by then), longer than about 6 s for `stop` -> `final`.
An exit with any code means "respawn"; sessions in flight at that moment get no
further events.

A fresh `AVAudioEngine` is created for every dictation, so a device change
*between* dictations needs no handling at all.

## Permissions

- **Microphone**: required. `status.mic` reports
  `AVCaptureDevice.authorizationStatus(for: .audio)`.
- **Speech Recognition**: not required. `SpeechTranscriber` runs fully on
  device; it transcribed normally here while
  `SFSpeechRecognizer.authorizationStatus()` was `.notDetermined`, and no
  prompt appeared. `status.speech` is therefore always `"not_required"`, and
  the helper never calls `SFSpeechRecognizer.requestAuthorization`.
- `request_permissions`: if the mic is undetermined the helper emits `status`
  immediately, shows the system prompt, and emits `status` again once it is
  answered. Otherwise it emits `status` once.
- `start` with the mic undetermined shows the prompt and waits; a `stop` or
  `cancel` in the meantime is answered at once.
- The prompt is attributed to the *responsible process*: freeflo.app when the
  app spawns the helper, the terminal when you run it by hand.

## Locales

`status.supported` lists what `SpeechTranscriber` can do (45 locales here,
including `en-IN`, `hi-IN` and `mul-IN`); `status.installed` what is on disk.
`start`/`prewarm` on a supported locale that is not installed answer
`locale_not_installed`; `install` then downloads it through `AssetInventory`
(system-managed, shared, not stored in the app). Installing an installed
locale answers `installed` (`already: true`) at once (45 ms measured), with no
progress events.

Downloads: `install` resolves the locale first (unknown -> `error`
`locale_unsupported` with `cmd: "install"`). A second `install` for the same
resolved locale while one is running joins it: from then on every event of
that download is sent once for each spelling the app used (`hi-IN`, `hi_IN`),
so every request is answered. Progress is reported as described above; a download whose fraction
has not changed for 180 s is cancelled and reported as `network`. One retry
on other failures (the asset service fails transiently when asked the moment
the network comes up), and the locale counts as installed if it landed even
though the call threw. A full per-app reservation table
(`AssetInventory.maximumReservedLocales`, 5) is resolved by releasing exactly
one reservation that is neither in use nor the one being installed (a
reservation only keeps the system from reclaiming an asset; releasing it
deletes nothing).

**Reservations are shared** by every process signed with the helper's
identifier (`com.freeflo.helper`), including an installed copy of the app.
`prewarm` reserves the locales it loads; `FREEFLO_NO_RESERVE=1` (and any
`FREEFLO_FAKE_MIC`) turns that off, and every test sets it.

**The India model.** On macOS 27.2 there is exactly one India speech asset
(`com.apple.speech.asr.transcription.mul-IN`, ~623 MB); every English locale
shares another (`transcription.en`, ~380 MB). Installing it flips all 15
non-English `xx-IN` locales to installed at once, and hi-IN, mul-IN, bn-IN,
ta-IN and ur-IN give byte-identical text (hi-IN vs mul-IN: identical text and
word confidences on 23 of 23 clips). `status.groups` reports this so the app
never offers two of them together. It **always writes in Latin letters**
(romanised Hindi: "kal raat ko server down ho gaya"; 0 of 23 clips contained
an Indic character, and there is no API option for the script), and it
follows English mixed into a sentence. These facts were measured on
macOS 27.2 only: on another macOS Hindi may be a separate model writing
Devanagari, which `status.groups` would then show.

Measured again for helper 1.2 on eight fresh clips (English by the `say` voice
Rishi, Hindi and Hinglish by Lekha, and concatenations of the two): hi-IN and
mul-IN gave byte-identical text on 8 of 8 and none contained a non-ASCII
character. "mul" is therefore not a language detector in front of several
models but one model that writes whatever it hears, Hindi or English, in
Latin letters. What it costs: on English it is a little worse than en-IN
("We will shape the new build" for "ship"), and once Hindi appears it stops
punctuating and capitalising.

**No romanising option.** There is nothing to romanise (the model never
writes Devanagari), `SpeechTranscriber` has no script option (its only
transcription option in the macOS 27.2 SDK is `etiquetteReplacements`), and
the system transliterator would not be good enough if it were needed:
`Any-Latin; Latin-ASCII` (ICU, the engine behind `CFStringTransform`) turns
"कल रात को सर्वर डाउन हो गया था" into "kala rata ko sarvara da'una ho gaya tha"
and "टेस्टिंग अभी पेंडिंग है" into "testinga abhi pendinga hai" (it keeps the
silent vowels and does not know English words), where the India model itself
writes "kal raat ko server down ho gaya tha".

## Two languages

A `SpeechTranscriber` is built for one locale. `en-IN` writes excellent,
punctuated English but turns Hindi into "Subhe, Das, Baji, Mujhe, Daftar,
Jana Hai"; the India model understands Hindi, English and Hinglish but drops
punctuation and sentence case once Hindi appears, and its English is a little
worse ("send it now" vs "Send it now."). So with `"languages":["en-IN","hi-IN"]`
**both recognisers run on the same audio inside one `SpeechAnalyzer`** and the
transcript is chosen sentence by sentence (`Blend.swift`):

1. The primary (the monolingual locale, en-IN) is cut into sentences after
   every `.`/`?`/`!` and at every result boundary.
2. Each word of the secondary (the India model) is assigned to a sentence by
   the **end** time of the word (both recognisers attach the pause before a
   word to that word, so start times are loose; end times are tight), or, if it
   ends in a gap the primary did not hear at all, to a run of its own (a lone
   word in a gap is jitter and goes to the nearer sentence).
3. A sentence goes to the secondary when the primary's mean word confidence is
   below 0.72 and the secondary's is higher ("floor"), or when it is below 0.86,
   the secondary is at least 0.02 surer and at least 20 % of the primary's
   words after the first are Capitalised the way en-* writes words it does not
   know ("margin"). Otherwise the primary's sentence is kept verbatim.
4. Two checks that do not depend on the confidences (which were measured on
   synthesised speech and are a thin margin: the surest Hindi sentence scored
   0.74 on en-IN, the least sure English one 0.78):
   - **lexicon**: when the secondary is the India model and what it wrote for
     the sentence is Hindi (at least 2 words from a list of about 240 everyday
     Hindi words as that model spells them, making up at least 30 % of the
     sentence), the sentence goes to the secondary even though the primary
     sounded sure, unless the primary is very sure (0.93 or more). On the 52
     recorded clips every English sentence had 0 or 1 such words (at most
     17 %) and every Hindi or Hinglish one 3 to 12 (36 % or more).
   - **same**: when the confidences say "hand over" but both recognisers wrote
     the same words (80 % or more in common), nothing looks foreign (few
     capitals, no Hindi), the sentence is in the primary's language and the
     primary's punctuated version is kept ("Okay, team." instead of
     "Okay team").
5. A word reported on both sides of a seam is written once; the secondary's
   stretches get sentence case and the primary's closing mark, and Hindi words
   the India model capitalised at random are written in lower case ("ho gaya
   Tha" -> "ho gaya tha", "Agle sprint" -> "agle sprint"; only words on the
   list, so names and English words keep their capitals).

Results on the measured clips (synthesised with the `say` voices Rishi, Lekha,
Piya, Vani and Geeta; 52 clips clean and at 10 dB SNR plus an 80 s mixed one):
every English sentence stays exactly as en-IN wrote it, every Hindi, Hinglish,
Bangla, Tamil or Telugu sentence comes from the India model, no word is
dropped or duplicated. For example:

| Clip | en-IN alone | en-IN + hi-IN |
| --- | --- | --- |
| English, Hindi, English | Okay, team, quick update on the release. Call Radco server down Hogeyatha is leave deployment through Gaya. We will retry tonight after the database migration. | Okay, team, quick update on the release. Kal raat ko server down ho gaya tha isliye deployment ruk gaya. We will retry tonight after the database migration. |
| Hindi | Aaj, Shamku make Bazar, Janga, or Sabzi, Laker, Janga, Kya, Tumhe, Kuch, Chahi? | Aaj shaam ko mei bazar jaaega aur sabji lekar jaaega kya tumhe kuch chahiye? |
| Hinglish | Mujhe Lagta, Hai, Ki Hame, Ye feature, Agle, Prince, Mein, Ship, Karna, ... | Mujhe lagta hai ki hamen ye feature agle sprint mein shift karna chahiye kyonki testing abhi pending hai. |
| Hinglish | Hi, meeting Kal shift, Kardo, client, Abhi, available, Nahi, Hai, or report Mujhe, email, Kardena. | Bhai meeting kal shift kar do client abhi available nahin hai aur report mujhe email kar dena. |
| English | Send it now. | Send it now. |

Cost (M4, macOS 27.2, fake mic at real-time pace): `stop` -> `final` 108-231 ms
for 1-11 s clips (en-IN alone 99-242 ms on the same clips), `prewarm` 80-90 ms,
`prepare` 42-50 ms, resident memory ~28 MB, about 1.1 s of CPU per 80 s of
audio. The texts are those of each recogniser running alone (asking for word
times now and then moves a comma in en-IN's text).

Measured again with helper 1.2 on five clips of 6-20 s, `stop` -> `final`:

| Clip | en-IN | hi-IN | en-IN + hi-IN |
| --- | --- | --- | --- |
| English, 7.8 s | 136 ms | 116 ms | 149 ms |
| Hindi, 5.9 s | 173 ms | 139 ms | 172 ms |
| Hinglish, 6.2 s | 237 ms | 130 ms | 254 ms |
| English, Hindi, English, 20.2 s | 152 ms | 244 ms | 234 ms |
| Hindi, English, Hinglish, 17.2 s | 230 ms | 413 ms | 475 ms |

The two recognisers finalise in parallel, so two languages cost about as much
as the slower of the two; the India model is the slower one after a long
stretch of Hindi, because it does not cut that into sentences while it
listens.

Details and limits:

- The running `partial` text comes from one recogniser at a time. With the
  India model as the secondary its own running words decide: as soon as three
  or more of them are there and they are Hindi (the rule above) the text
  follows the India model (measured: from the first batch of partials, 4 s
  into a Hindi dictation, instead of "Ru server"), and it goes back to the
  primary when a stretch has no Hindi word at all. Otherwise (and in between)
  the confidences of the primary's finished sentences decide, as before:
  below 0.72 over 3+ words -> the secondary. It can differ from the `final`
  text.
- The India model's missing commas remain, and so do its stray capitals on
  words that are not on the list; sentence case, the closing mark and lower
  case for the listed words are all that is added.
- The word list is Hindi only. Bangla, Tamil, Telugu and the other languages
  of the India model are decided by the confidences alone.
- The thresholds come from synthetic speech; each dictation logs one
  text-free line (`blend: N sentences, kept K (n by same words), handed over ... primary
  confidence min/mean ...`) for tuning from real use.
- If the two-language pipeline cannot be built for a reason other than the
  languages themselves, the dictation continues with the primary alone and
  the other language is listed in `started.missing`.
- `helper/tests/blend_ref.py` is the reference the rules were tuned with;
  `blend_test.py --ref` checks that it and the Swift port agree on all 41
  fixtures (bit-identical, including every number of every decision) and that
  the Hindi word list is the same in both.
- None of this has been measured on a human voice: every clip was
  synthesised. The per-dictation log line is there to check it against real
  use.

## Vocabulary (`context`)

`context` strings are applied as `AnalysisContext.contextualStrings[.general]`.
The API accepts them, but on macOS 27.2 they have **no measurable effect on
`SpeechTranscriber`** (tried with made-up names, both via `setContext` and at
analyzer creation: output identical with and without). The older
`DictationTranscriber` does honour them, at lower overall accuracy, and is not
used. So: keep sending `context` (it costs ~2 ms and may start working in a
later OS), but do custom-word correction in the app.

## Performance

Measured on an Apple M4, macOS 27.2, `en-IN`. "Streamed" rows feed a speech
clip through the fake microphone at real-time pace, which exercises exactly
the path live audio takes.

| | |
| --- | --- |
| `prewarm` (model already cached by the system) | 25-50 ms |
| `start` -> `started`, built-in mic | 80-110 ms (almost all of it AVAudioEngine opening the device) |
| `stop` -> `final`, streamed 1-12 s dictation | 90-190 ms |
| `stop` -> `final`, streamed 70 s dictation | 224 ms |
| `stop` -> `final`, built-in mic (silent input) | 50-200 ms |
| `transcribe-file`, 11.6 s clip | 230-350 ms |
| `transcribe-file`, 69.6 s clip | 0.82 s |
| Memory, idle with model warm / during a 70 s session | ~20 MB / ~20 MB |

Audio is streamed into the analyzer while the user speaks, so at `stop` only
the last unfinalised stretch remains. On a real microphone `stop` -> `final`
breaks down as: up to 100 ms waiting for the tap buffer in flight (macOS
delivers input taps in 100 ms buffers whatever size is requested), ~10 ms to
stop the engine, 50-150 ms to finalise.

Prewarming: analyzers are created with `modelRetention: .processLifetime` and
prepared with `prepareToAnalyze(in:)`. After `prewarm`, and again after every
dictation, a prepared analyzer is kept ready for the next `start`. Measured on
the 11.6 s clip: first result after 20 ms with a prepared analyzer versus
45-170 ms without. The microphone is opened in parallel with attaching the
analyzer, so a cold model never delays or loses audio.

Reporting options (11.6 s clip, real-time):

| `partials` | `fast_partials` | Partial text | Final text |
| --- | --- | --- | --- |
| false | - | none | reference |
| true | false (default) | refreshed about every 4 s of speech | identical to reference |
| true | true | refreshed about every second | punctuation drifted (one comma lost) |

## Test hooks

Environment variables, for tests only:

| Variable | Effect |
| --- | --- |
| `FREEFLO_FAKE_MIC=<file>[:<file>...]` | Play audio files at real-time pace instead of opening the mic (then silence). Mic permission is reported as granted. Several files in different formats simulate a device switch. |
| `FREEFLO_FAKE_MIC_SPEED=8` | Play the fake mic faster than real time. |
| `FREEFLO_FAKE_MIC=@none` | No input device: `start` answers `no_input_device`. |
| `FREEFLO_FAKE_MIC=@denied` | Mic permission denied: `status.mic` is `denied`, `start` answers `mic_denied`. |
| `FREEFLO_FAKE_MIC=@hang` | Opening the mic never returns (exercises the 4 s watchdog and exit 75). |
| `FREEFLO_FAKE_MIC=@hang-stop` with `FREEFLO_FAKE_MIC_FILE=<file>` | Capture works but closing it never returns. |
| `FREEFLO_TEST_LOSE_MIC=<seconds>` | The fake mic disappears for good after that long. |
| `FREEFLO_TEST_CONFIG_CHANGE=<seconds>` | Real mic: post `AVAudioEngineConfigurationChange` after that long, as a device switch would. |
| `FREEFLO_NO_RESERVE=1` | Never touch the shared asset reservation table (also implied by any `FREEFLO_FAKE_MIC`). |
| `FREEFLO_TEST_INSTALL=ok:<sec>` | `install` simulates a download whose fraction goes 0 -> 1 over `<sec>`, then succeeds. No `AssetInventory` call; the requested locale counts as not installed until then, and as installed afterwards (in this process only). |
| `FREEFLO_TEST_INSTALL=stall` | The simulated download never moves (exercises stall detection). |
| `FREEFLO_TEST_INSTALL=fail:<code>` | The simulated download fails with `<code>` after 0.3 s. |
| `FREEFLO_INSTALL_STALL_SEC=<sec>` | Stall limit instead of 180 s. |

`helper/tests/smoke_test.py` uses these (plus clips synthesised with `say`) to
check the whole protocol without touching the microphone, the reservation
table or the network (79 checks; the only real `install` it sends is for
locales that are already installed, which answers at once and downloads
nothing). `--slow` adds the hang scenarios, `--mic` one
real-microphone dictation. Its two-language checks need hi-IN installed and
the voices Rishi and Lekha, and are skipped with a note otherwise.

## Notes for packaging

- Ship the binary inside the app bundle and spawn it directly from the app so
  the microphone prompt and the privacy indicator are attributed to freeflo.
- When the bundle is signed with the hardened runtime, sign the helper with
  `helper/entitlements.plist` (`com.apple.security.device.audio-input`); it
  needs no other entitlement.
- Drain or redirect the helper's stderr. Log lines are short and rate-limited
  and never contain transcript text or vocabulary, but stdout/stderr are pipes.
- Minimum macOS for the app becomes 26 (`LSMinimumSystemVersion`).
