A test that sends `/entities/..` to the application never tests that path: the client resolves the dot segments first and requests `/`, so write the segment as `%2e%2e`.

Seen in WP-17 (2026-11-18, Starlette's `TestClient` on httpx). The parametrised "a name that is not an object name is the 404" test passed for `/metrics/..%2Fetc%2Fpasswd` and failed for `/entities/..` with a 200: the start page. With `/entities/%2e%2e` the application receives the name `..`, which `ID_PATTERN` refuses (a name must start with a letter, a digit or an underscore).
