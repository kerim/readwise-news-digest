// The program's notifications, sent under its own name.
//
// Built by build-launchers.sh into bin/Readwise News Digest.app, so macOS lists
// it as "Readwise News Digest" in System Settings → Notifications (osascript's
// notifications are filed under Script Editor, shared with every other script).
// Usage: readwise-news-digest-notify TITLE MESSAGE. Exits 0 once the notification
// is handed to macOS, 1 if notifications are not allowed (common.notify then
// falls back to osascript). macOS may ask you to allow it the first time.

#import <Foundation/Foundation.h>
#import <UserNotifications/UserNotifications.h>

int main(int argc, const char *argv[]) {
  @autoreleasepool {
    if (argc < 3) { fprintf(stderr, "usage: notifier TITLE MESSAGE\n"); return 2; }
    NSString *title = @(argv[1]), *body = @(argv[2]);
    UNUserNotificationCenter *center = [UNUserNotificationCenter currentNotificationCenter];
    dispatch_semaphore_t done = dispatch_semaphore_create(0);
    __block int rc = 0;
    [center requestAuthorizationWithOptions:UNAuthorizationOptionAlert | UNAuthorizationOptionSound
                          completionHandler:^(BOOL granted, NSError *error) {
      if (!granted) {
        fprintf(stderr, "not allowed: %s\n", error ? error.localizedDescription.UTF8String : "turned off");
        rc = 1; dispatch_semaphore_signal(done); return;
      }
      UNMutableNotificationContent *content = [UNMutableNotificationContent new];
      content.title = title; content.body = body;
      UNNotificationRequest *request = [UNNotificationRequest
          requestWithIdentifier:[[NSUUID UUID] UUIDString] content:content trigger:nil];
      [center addNotificationRequest:request withCompletionHandler:^(NSError *e) {
        if (e) { fprintf(stderr, "%s\n", e.localizedDescription.UTF8String); rc = 1; }
        dispatch_semaphore_signal(done);
      }];
    }];
    if (dispatch_semaphore_wait(done, dispatch_time(DISPATCH_TIME_NOW, 120 * NSEC_PER_SEC))) {
      fprintf(stderr, "timed out\n"); return 1;
    }
    return rc;
  }
}
