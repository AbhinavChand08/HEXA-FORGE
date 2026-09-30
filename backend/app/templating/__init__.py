"""
Templating: flat records -> command templates with typed slots (design section 7).

    ip ssh version 2                          ->  ip ssh version <NUM>
    interface Gi0/1 > ip address 10.1.1.1 255.255.255.0
                                              ->  [interface <IFNAME>] ip address <IPV4> <MASK>
    snmp-server community public RO           ->  snmp-server community <WORD> <ENUM>  {RO, RW}
    <MinimumPasswordLengthValue>14</..>       ->  ... > MinimumPasswordLengthValue <NUM>

  lexer.py      typed tokens; typed_pattern() is the runtime (no mining) templatizer
  miner.py      offline Drain-based slot mining over a corpus (TemplateLearner)
  signature.py  rendering, stable signatures, slot value extraction, file signatures
  hints.py      LearnerHints: every knob is data (hints.json), not code

Templating never reads the registry; the vendor profile is an argument.
"""
