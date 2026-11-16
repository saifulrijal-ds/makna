To tamper with a signed cookie in a test, change the first character of the signature, not the last: the last base64 character of a 32-byte signature carries two unused bits, so changing it can decode to the same bytes and the cookie still verifies.

Seen in WP-16 (2026-11-16): a test that flipped the last character of an itsdangerous cookie passed on most runs and failed on some, because the new last character sometimes decoded to the same signature.
