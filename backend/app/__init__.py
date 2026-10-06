# Trust the operating system's certificate store rather than only the CA bundle Python ships.
#
# On a network that inspects HTTPS (here, Check Point Harmony SASE) every certificate is
# re-signed by the company's own root CA. IT installs that CA into the Windows store, so browsers
# accept it, but httpx verifies against certifi's bundle, which has never heard of it -- so every
# outbound call failed with "certificate verify failed" and OFF-01/OFF-02 came back UNKNOWN as
# "Source unavailable". Done here, before anything opens a connection, so the server, the
# snapshot script and the tests all get it. Verification stays on; only the trusted set changes.
try:
    import truststore

    truststore.inject_into_ssl()
except ImportError:
    pass
