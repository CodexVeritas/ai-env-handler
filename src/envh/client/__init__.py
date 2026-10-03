"""The untrusted side: what you or an agent run as your own user. Standard library only.

It can only ask the broker over its socket; it holds no secrets and no policy. A test keeps it free of envh.core,
envh.server and third-party imports so it can be copied into a container unchanged. Entry point: envh.client.commands.
"""
