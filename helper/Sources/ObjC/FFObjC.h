// Objective-C exception bridge for freeflo-helper.
//
// AVAudioEngine reports several failure modes (no input device, a tap format
// that no longer matches the hardware after a device change, ...) by raising
// NSException, which Swift cannot catch and which would otherwise kill the
// helper. FFTryObjC turns those exceptions into NSError so Swift can `try` it.
#import <Foundation/Foundation.h>

NS_ASSUME_NONNULL_BEGIN

FOUNDATION_EXPORT NSErrorDomain const FFObjCExceptionDomain;

/// Runs `block`. Returns YES on success. If the block raises an Objective-C
/// exception, returns NO and fills `error` (domain FFObjCExceptionDomain,
/// localizedDescription "<name>: <reason>").
BOOL FFTryObjC(NS_NOESCAPE void (^block)(void), NSError *_Nullable *_Nullable error);

NS_ASSUME_NONNULL_END
