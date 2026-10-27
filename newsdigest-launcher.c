/*
 * newsdigest-launcher.c — generic passthrough launcher.
 *
 * Exists purely so macOS shows a meaningful name for this project's launchd
 * jobs. Background Task Management (System Settings > Login Items &
 * Extensions) names a job after the basename of its ProgramArguments[0], so a
 * plist that starts with /bin/bash or /usr/bin/env shows up as "bash" or
 * "env". Nothing the process does at runtime changes that: BTM reads the
 * registered executable path, not the running process's argv[0].
 *
 * So this program is compiled once per job under the job's own name
 * (readwise-news-digest-run, readwise-news-digest-profile) and placed at
 * ProgramArguments[0]. It immediately exec()s the real command, which is
 * passed as argv[1..]. The real command's own argv[0] is replaced with this
 * launcher's filename, so `ps` and Activity Monitor agree with BTM.
 *
 * ProgramArguments[0] must still never be a shell script: AppleSystemPolicy
 * kills a provenance-tagged script spawned directly by launchd
 * (OS_REASON_CODESIGNING). A compiled Mach-O binary is fine — the linker
 * ad-hoc-signs it on Apple Silicon.
 *
 * Build with build-launchers.sh.
 */
#include <errno.h>
#include <libgen.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>

int main(int argc, char *argv[]) {
    const char *self = (argc > 0 && argv[0] != NULL) ? argv[0] : "newsdigest-launcher";

    if (argc < 2) {
        fprintf(stderr, "usage: %s <absolute-command> [args...]\n", self);
        return 2;
    }

    /* basename() may modify its argument, so work on a copy. */
    char selfcopy[1024];
    snprintf(selfcopy, sizeof selfcopy, "%s", self);
    char *name = basename(selfcopy);

    /* execv() takes a real path — no PATH search, deliberately. Overwriting
     * argv[1] with our own name turns &argv[1] into the child's argv, with
     * argv[0] = our name and every later argument untouched. */
    char *command = argv[1];
    argv[1] = name;
    execv(command, &argv[1]);

    fprintf(stderr, "%s: cannot exec %s: %s\n", name, command, strerror(errno));
    return 127;
}
