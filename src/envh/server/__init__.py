"""The broker process edges, running as user envh: the Unix socket, the console terminal, hardening, startup, init.

Everything here is on the trusted side of the boundary. It never imports envh.client or envh.tools.
"""
