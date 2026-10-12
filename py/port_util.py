"""Port selection for the server: is a port free, is TrioForge already on it, pick the next one."""
import urllib.request


def port_is_free(port, host='127.0.0.1', timeout=0.6):
    import socket as _s
    s = _s.socket(_s.AF_INET, _s.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        return s.connect_ex((host, port)) != 0
    except Exception:
        return True
    finally:
        try:
            s.close()
        except Exception:
            pass


def is_trioforge_on(port, host='127.0.0.1', timeout=1.5):
    """True only if TrioForge itself answers on this port (via /api/ping).

    Tries BOTH schemes. On Windows TrioForge serves HTTPS by default, and while
    this only spoke http the check never matched a running instance - so a second
    launch decided the port was "busy with another app", picked 5004, then 5005,
    and every launch piled up another server, another console window and another
    firewall prompt.
    """
    import json as _json
    import urllib.request
    import ssl as _ssl
    ctx = _ssl._create_unverified_context()
    for scheme in ('http', 'https'):
        try:
            with urllib.request.urlopen('%s://%s:%d/api/ping' % (scheme, host, port),
                                        timeout=timeout, context=ctx) as r:
                data = _json.loads(r.read(400).decode('utf-8', 'replace'))
            if data.get('app') == 'trioforge':
                return True
        except Exception:
            continue
    return False


def choose_port(preferred, host='127.0.0.1', tries=20):
    """Return (port, reason).

    reason: 'free'    â†’ nothing listening; use it
            'running' â†’ TrioForge is already there (open the browser, then exit)
            'moved'   â†’ another app owns it; moved to the next free port
            'busy'    â†’ no free port found in range
    """
    if port_is_free(preferred, host):
        return preferred, 'free'
    if is_trioforge_on(preferred, host):
        return preferred, 'running'
    for p in range(preferred + 1, preferred + 1 + tries):
        if port_is_free(p, host):
            return p, 'moved'
    return preferred, 'busy'
