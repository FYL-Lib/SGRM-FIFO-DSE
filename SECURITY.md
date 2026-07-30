# Security

## Trace trust boundary

LightningSim `trace.pkl` files use Python pickle serialization. Loading an
untrusted pickle can execute arbitrary code with the privileges of the current
user. SGRM therefore treats a trace bundle as executable input, not as a safe
data-only document.

Use trace bundles only when their source is trusted. The manifest-driven runner
passes every entry's SHA-256 digest to the single-trace command, which verifies
the file before constructing the LightningSim backend. The published archive
also has an archive-level digest in `datasets/SHA256SUMS`.

A checksum protects integrity relative to the trusted manifest; it does not
make an unknown pickle safe. Run third-party traces in a suitably isolated
environment.

## Reporting a vulnerability

Open a private security advisory in the GitHub repository rather than posting
exploit details in a public issue.
