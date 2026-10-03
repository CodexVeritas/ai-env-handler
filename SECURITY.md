# Security policy

## Reporting a vulnerability

Report privately through GitHub: open the repository's **Security** tab and choose **Report a vulnerability**. Please do not open a public issue for anything that could help someone get around an approval.

A useful report says what you ran, as which user, what you expected, what happened, and the commit you tested.

## What counts

In scope: any way for a process running as a listed user (an agent, a script, a dependency) to read a secret, approve or forge an approval, change the policy or presets, or read the audit log without the console approving it; any way for another local account to use the socket or someone else's session; anything that makes the installer leave a weaker setup than the README describes.

Out of scope: the limits listed under [What envh does not do](README.md#what-envh-does-not-do), such as the approved command seeing its values, or a program running as you reaching the console through an X11 display or a writable `/dev/uinput` ([Who else can see and type into the console](README.md#who-else-can-see-and-type-into-the-console)).

## Supported versions

Fixes go to the latest commit on `main`. envh has not had an independent security review.
