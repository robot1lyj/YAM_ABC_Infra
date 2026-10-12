"""Trust configured names and, optionally, the actual local IPv4 socket address.

The socket destination comes from the ASGI server, never Host/forwarded headers.
This permits DHCP changes without accepting arbitrary DNS names or stale IPs.
"""

from ipaddress import AddressValueError, IPv4Address

from starlette.middleware.trustedhost import TrustedHostMiddleware


class LocalAddressTrustedHostMiddleware:
    def __init__(self, app, *, allowed_hosts, allow_local_addresses=False):
        self.app = app
        self.allowed_hosts = tuple(allowed_hosts)
        self.allow_local_addresses = allow_local_addresses
        self.fixed = TrustedHostMiddleware(app, allowed_hosts=self.allowed_hosts)

    async def __call__(self, scope, receive, send):
        if self.allow_local_addresses and scope["type"] in ("http", "websocket"):
            try:
                address = IPv4Address(scope["server"][0])
            except (AddressValueError, KeyError, TypeError, IndexError):
                pass  # Fail closed if the server cannot provide a concrete IPv4 destination.
            else:
                if not address.is_unspecified:
                    dynamic = TrustedHostMiddleware(
                        self.app,
                        allowed_hosts=[*self.allowed_hosts, str(address)],
                        www_redirect=False,
                    )
                    return await dynamic(scope, receive, send)
        return await self.fixed(scope, receive, send)
