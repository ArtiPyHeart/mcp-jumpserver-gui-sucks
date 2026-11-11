# JumpServer Access

This glossary distinguishes credentials used for discovery from the authenticated
session required to access assets through JumpServer.

## Language

**Durable REST credential**:
A JumpServer Access Key used for authenticated REST requests until it expires or
is revoked. It does not by itself establish a terminal web session.

**Terminal web session**:
A cookie-backed JumpServer login session accepted by KoKo for terminal and SFTP
access.

**Session refresh**:
A check of an existing terminal web session that can extend its validity when
JumpServer permits it. It cannot recreate an invalid session.

**Fresh login**:
Authentication with the JumpServer username, password, and any server-required
MFA or captcha challenges to establish a new terminal web session.
