"""Small HTML tree for nested homework content and list rows."""

from html import escape
from html.parser import HTMLParser

VOID_TAGS = frozenset(
    "area base br col embed hr img input link meta param source track wbr".split()
)


class Node:
    def __init__(self, tag="", attrs=(), parent=None):
        self.tag = tag
        self.attrs = dict(attrs)
        self.parent = parent
        self.children = []

    def has_class(self, name):
        return name in (self.attrs.get("class") or "").split()

    def walk(self, tag=None):
        for child in self.children:
            if isinstance(child, Node):
                if tag is None or child.tag == tag:
                    yield child
                yield from child.walk(tag)

    def text(self):
        if self.tag in {"script", "style", "template"}:
            return ""
        return " ".join(
            " ".join(
                child.text() if isinstance(child, Node) else child for child in self.children
            ).split()
        )

    def inner_html(self):
        return "".join(
            child.html() if isinstance(child, Node) else escape(child) for child in self.children
        )

    def html(self):
        attrs = "".join(
            f' {key}="{escape(value, quote=True)}"' if value is not None else f" {key}"
            for key, value in self.attrs.items()
        )
        start = f"<{self.tag}{attrs}>"
        return start if self.tag in VOID_TAGS else start + self.inner_html() + f"</{self.tag}>"


class _TreeParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node()
        self.current = self.root

    def handle_starttag(self, tag, attrs):
        node = Node(tag, attrs, self.current)
        self.current.children.append(node)
        if tag not in VOID_TAGS:
            self.current = node

    def handle_startendtag(self, tag, attrs):
        self.current.children.append(Node(tag, attrs, self.current))

    def handle_endtag(self, tag):
        node = self.current
        while node.parent is not None:
            if node.tag == tag:
                self.current = node.parent
                break
            node = node.parent

    def handle_data(self, data):
        self.current.children.append(data)


def parse_html(source):
    parser = _TreeParser()
    parser.feed(source)
    parser.close()
    return parser.root
