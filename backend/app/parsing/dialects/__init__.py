"""One parser per dialect. Each implements parsing.base.DialectParser."""
from app.parsing.dialects.brace import parse_brace
from app.parsing.dialects.indent import parse_indent
from app.parsing.dialects.ini import parse_ini
from app.parsing.dialects.setstyle import parse_set
from app.parsing.dialects.tree import parse_json, parse_xml, parse_yaml

PARSERS = {"indent": parse_indent, "brace": parse_brace, "set": parse_set,
           "xml": parse_xml, "json": parse_json, "yaml": parse_yaml, "ini": parse_ini}
