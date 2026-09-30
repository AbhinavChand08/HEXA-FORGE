"""
Parsing: any configuration -> flat structured records (design section 6).

The parent lines are kept as a path, because they carry the scope of the child
(which interface, which line, which policy):

    {"device_id": "rtr-01", "path": ["line con 0", "exec-timeout 10 0"],
     "negated": false, "src_line": 142, "tokens": [...], "roles": ..., ...}

Parsers exist per DIALECT, never per vendor (parsing/dialects/):

  indent  indentation + terminators          Cisco IOS, Arista EOS, Huawei VRP,
                                             FortiOS, MikroTik ...
  brace   { } blocks                         Juniper, F5 BIG-IP, Nokia MD-CLI ...
  set     one statement per line (flat)      Junos / VyOS / PAN-OS set format
  xml     \\
  json     > tree paths; list items are      Sophos, Palo Alto, SONiC, NVUE,
  yaml    /  keyed by their name             API exports
  ini     [section] + key = value            appliances, Linux daemons

A VENDOR PROFILE is a data row (registry-seed/vendors/<vendor>/profile.yaml) that
picks a dialect and fills in its knobs (DialectHints). Parsing receives the profile
as an argument and never reads the registry.

Always done here, before anything is stored or learned:
  * negation prefixes (no / undo / delete / unset) are stripped and carried as
    negated=true, so 'no ip http server' and 'ip http server' share a template
  * secrets (passwords, hashes, keys, PEM blobs, certificates) become <SECRET>
"""
