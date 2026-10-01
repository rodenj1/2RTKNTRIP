# Changelog

## 2.3.0

NTRIP conformance and reliability release. The caster now follows NTRIP 1.0/2.0 more closely, so it works with strict clients and servers such as BKG's `ntripclient`/`ntripserver` and sp-rtk-base-relay. A number of connection-handling bugs are also fixed.

### New setting

- **`ntrip.mount_data_timeout`** (default 30 s): a mount whose source has sent no data for this long is treated as gone. Downloads get the unavailable reply, and a new upload can take the mount over. (#43, #44)

### Behaviour changes to know about when upgrading

- **Version detection** comes only from the method (`SOURCE` is v1, `POST` is v2) and the `Ntrip-Version: Ntrip/2.0` header, matched case-insensitively. The User-Agent and HTTP version no longer decide it, so every other `GET` is v1. (#28)
- **A browser `GET /`** now gets the v1 sourcetable instead of the HTML landing page. (#28)
- **Every `POST` is an NTRIP 2.0 upload,** so it needs a `Host` header. Its body is de-chunked only if it says `Transfer-Encoding: chunked`. (#28, #38)
- **An unknown mount, or one with no source online,** is answered with the sourcetable (v1) or 404 (v2) before authentication, instead of 401. (#33)
- **v1 error replies:**
  - Clients get `HTTP/1.0` status lines.
  - Sources (`SOURCE`) get `ERROR - Bad Password` and `ERROR - Mount Point Taken or Invalid`.
  - `SOURCETABLE 401` and `ERROR <code>` are no longer sent.

  (#33)
- **A same-IP reconnect** replaces the old session only after the new one has authenticated. (#44)
- **RTSP:**
  - Errors are `RTSP/1.0` replies, and an unimplemented method gets 501.
  - PLAY and RECORD send exactly one reply.
  - `OPTIONS` (including `OPTIONS *`) is answered with a `Public` header.
  - PLAY authenticates with download credentials.

  (#40, #48)

### Fixes

- **v2 reply headers:**
  - `Ntrip-Version: Ntrip/2.0` instead of `NTRIP/2.0`
  - `Content-Type: gnss/data` for streams
  - `gnss/sourcetable` for sourcetables

  (#30)
- **A source reconnecting quickly from the same IP** is no longer torn down by the old session's delayed cleanup. (#35)
- **A reconnect no longer stalls the whole caster for ~5 s** while the old session's STR parser stops. The parser now stops at once, outside the mount lock. (#51)
- **Download slots:**
  - A closed download releases its per-user slot at once, including on error paths.
  - One session's release can no longer free another's.

  (#36)
- **Body bytes that arrive with the request headers are kept.**
  - Headers split across reads are read in full.
  - Legacy requests (LF-only, or no blank line) are still served.
  - An oversized header block is rejected.

  (#47)
- **The netstat-based zombie cleanup on uploads is replaced** by the data-idle check. It spawned a subprocess per upload, ignored its own arguments, and could evict live sources. (#44)
- **Mount statistics** count each uploaded chunk once instead of twice. (#45)
- **An old session's STR correction** no longer stops or overwrites a reconnected session's parse. It also no longer kills the web view's live RTCM parser. (#46)
- **Invalid RTCM frames** are logged as one WARNING per mount per minute, instead of one ERROR line per frame. (#52)

### Build

- Pushing a `v*` tag also builds the Docker image and tags it with the version (e.g. `ghcr.io/rodenj1/2rtkntrip:2.3.0` and `:2.3`).
