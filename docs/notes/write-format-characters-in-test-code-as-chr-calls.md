Write bidirectional and zero-width characters in test code as `chr(0x202E)`, never as an escape or a raw character

A `"‮"` written through a tool or formatter can land in the file as the raw character, which a reader cannot see and a code review cannot trust (the "trojan source" problem). In WP-19B the first draft of `tests/integration/test_web_review.py` held two raw format characters this way. Build them with `chr(0x202E)` and `chr(0x200B)` instead, and check a new test file with `grep -c $'‮' <file>` (or a scan for category `Cf`) before committing.

- The same holds for the expected text: `"<U+202E>"` is how the review pages write such a character, and a test compares against that, not against the character.
- `check_reason` refuses every character of a category starting with `C`, so a reason can never hold one; to test that a page marks them in an audit row, insert the row directly, or put the character in a definition's description or in a commit's subject.
