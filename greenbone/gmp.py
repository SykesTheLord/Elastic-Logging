"""
A minimal Greenbone Management Protocol client. Standard library only.

GMP is XML over a stream socket: send one command element, read back exactly
one response element whose `status` attribute is an HTTP-style code. That is
all python-gvm does underneath, and the target VMs have no guaranteed pip
packages, so this is the dozen lines of it this repo needs.

    with Gmp("/run/gvmd/gvmd.sock") as g:
        g.authenticate("admin", password)
        for task in g.command("<get_tasks/>").iter("task"):
            ...

Used by provision.py (setup) and gvm-export.py (shipping results). Both run on
the Greenbone VM as root, which can open the socket docker-compose.yml binds to
/run/gvmd.
"""
import socket
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape, quoteattr

SOCKET = "/run/gvmd/gvmd.sock"


class GmpError(RuntimeError):
    def __init__(self, command, status, text):
        self.status = status
        super().__init__("%s -> %s %s" % (command, status, text))


def x(text):
    """Escape text for an element body."""
    return escape(str(text))


def attr(text):
    """Escape and quote text for an attribute value."""
    return quoteattr(str(text))


class Gmp:
    def __init__(self, path=SOCKET, timeout=600):
        self.path, self.timeout = path, timeout
        self.sock = None

    def __enter__(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.path)
        return self

    def __exit__(self, *exc):
        if self.sock:
            self.sock.close()
            self.sock = None

    def _read(self):
        """Read exactly one complete XML element off the socket.

        There is no length prefix or terminator: the response ends when its
        root element closes. A pull parser tells us when that has happened, so
        a large report arriving in many recv() chunks is handled the same way
        as a one-line status.
        """
        parser = ET.XMLPullParser(events=("start", "end"))
        depth = 0
        while True:
            chunk = self.sock.recv(1 << 16)
            if not chunk:
                raise ConnectionError("gvmd closed the connection mid-response")
            parser.feed(chunk)
            for event, elem in parser.read_events():
                depth += 1 if event == "start" else -1
                if depth == 0:
                    return elem

    def command(self, xml, ok=(200, 201, 202)):
        """Send one command, return the response element, raise on failure."""
        self.sock.sendall(xml.encode("utf-8"))
        resp = self._read()
        status = int(resp.get("status", "0"))
        if status not in ok:
            name = xml.lstrip("<").split(None, 1)[0].split(">", 1)[0].rstrip("/")
            raise GmpError(name, status, resp.get("status_text", ""))
        return resp

    def authenticate(self, username, password):
        self.command("<authenticate><credentials><username>%s</username>"
                     "<password>%s</password></credentials></authenticate>"
                     % (x(username), x(password)))

    def find(self, kind, name):
        """The id of the one <kind> called exactly `name`, or None.

        GMP filters match substrings unless the value is quoted with `=`, and
        even then several objects may share a name, so the match is re-checked
        here and the first exact one wins.
        """
        resp = self.command("<get_%ss filter=%s/>"
                            % (kind, attr('name="%s" rows=-1' % name)))
        for el in resp.findall(kind):
            if (el.findtext("name") or "") == name:
                return el.get("id")
        return None


def created_id(resp):
    """The id a create_* command returned."""
    return resp.get("id")
