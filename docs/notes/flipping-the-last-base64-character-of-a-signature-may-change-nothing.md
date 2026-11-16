To tamper with a signed cookie in a test, change the first character of the signature, not the last: the last base64 character of a signature carries two unused bits (32 bytes of HMAC-SHA256 make 43 characters, 258 bits, and 20 bytes of the default SHA-1 make 27 characters, 162 bits), so changing it can decode to the same bytes and the cookie still verifies.

Seen in WP-16 (2026-11-16): a test that flipped the last character of an itsdangerous cookie passed on most runs and failed on some, because the new last character sometimes decoded to the same signature.
