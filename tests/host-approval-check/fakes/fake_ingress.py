"""A fake ingress with a sign-in in front of the gateway of the product, for
the rows of the Control UI path (task 6.52).

It takes each connection on port 8080 and forwards it to the gateway of the
product (UPSTREAM, host:port). On the request head it does what the ingress
and the sign-in proxy of the platform do for a signed-in user: it removes
each copy of the identity and forwarded headers that the client sent, and
sets the user header to the one signed-in user (LAB_USER), with
X-Forwarded-For (the address of the client), X-Forwarded-Proto and
X-Forwarded-Host. After the head, it copies the bytes both ways, so a
WebSocket upgrade works.

Standard library only.
"""

import asyncio
import os

UPSTREAM = os.environ.get("UPSTREAM", "agent:18789")
USER_HEADER = os.environ.get("USER_HEADER", "x-auth-request-email")
LAB_USER = os.environ["LAB_USER"]
HOST = os.environ.get("FORWARDED_HOST", "ui.lab.invalid")
DROP = {
    USER_HEADER,
    "x-forwarded-for",
    "x-forwarded-proto",
    "x-forwarded-host",
    "x-real-ip",
    "forwarded",
}


async def pipe(reader, writer):
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    except (ConnectionError, asyncio.IncompleteReadError):
        pass
    finally:
        writer.close()


async def handle(client_reader, client_writer):
    try:
        head = await client_reader.readuntil(b"\r\n\r\n")
    except (asyncio.IncompleteReadError, asyncio.LimitOverrunError):
        client_writer.close()
        return
    lines = head.decode("latin-1").split("\r\n")
    request, fields = lines[0], [x for x in lines[1:] if x]
    kept = [x for x in fields if x.split(":", 1)[0].strip().lower() not in DROP]
    peer = client_writer.get_extra_info("peername")[0]
    kept += [
        f"{USER_HEADER}: {LAB_USER}",
        f"X-Forwarded-For: {peer}",
        "X-Forwarded-Proto: https",
        f"X-Forwarded-Host: {HOST}",
    ]
    host, port = UPSTREAM.rsplit(":", 1)
    try:
        up_reader, up_writer = await asyncio.open_connection(host, int(port))
    except OSError:
        client_writer.write(
            b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
        )
        await client_writer.drain()
        client_writer.close()
        return
    up_writer.write(("\r\n".join([request, *kept]) + "\r\n\r\n").encode("latin-1"))
    await up_writer.drain()
    await asyncio.gather(pipe(client_reader, up_writer), pipe(up_reader, client_writer))


async def main():
    server = await asyncio.start_server(handle, "0.0.0.0", 8080)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
