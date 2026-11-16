#import "FFObjC.h"

NSErrorDomain const FFObjCExceptionDomain = @"freeflo.helper.objc-exception";

BOOL FFTryObjC(NS_NOESCAPE void (^block)(void), NSError **error) {
    @try {
        block();
        return YES;
    } @catch (NSException *exception) {
        if (error) {
            NSString *reason = exception.reason ?: @"(no reason)";
            NSString *text = [NSString stringWithFormat:@"%@: %@", exception.name, reason];
            *error = [NSError errorWithDomain:FFObjCExceptionDomain
                                         code:1
                                     userInfo:@{NSLocalizedDescriptionKey: text}];
        }
        return NO;
    }
}
