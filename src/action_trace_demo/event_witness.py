"""Compare a financing action with its quoted clause, including its factual status."""
import re

from .native_grounding import dates


_EXECUTION = re.compile(r"\b(?:signed|executed|approved|waived|funded)\b", re.I)
_WITHDRAWAL = re.compile(r"\b(?:withdraw(?:n|al)?|withdrew|cancel(?:led|ed)|revoked|rescinded|已撤销|予以撤销|作废)\b", re.I)
_CONDITION = re.compile(r"\b(?:subject to|conditional (?:on|upon)|requir(?:e|es) (?:credit|internal) approval)\b", re.I)
PREDICATES = {"execution": _EXECUTION, "withdrawal": _WITHDRAWAL, "conditional": _CONDITION}
# Keep subordinate conditions with their main action. Separate explicit contrast,
# sentence boundaries and a conjunction that introduces a second named subject.
_CLAUSES = re.compile(r"(?:[!?;。！？；\n]+|\.(?=\s|$)|\s+\b(?:but|whereas|while|however)\b\s+|"
                      r",?\s+and\s+(?=(?:the\s+|[A-Z])[^.,;!?]{1,55}?\b(?:has|was|is|were|are|had)\b))", re.I)
_NEGATED = re.compile(r"\b(?:not|never|no longer|no evidence (?:of|that)|no confirmation (?:of|that)|"
                      r"without (?:any )?(?:evidence|confirmation) (?:of|that)|yet to be)\b|n['’]t\b", re.I)
_CONTINGENT = re.compile(r"\b(?:if|unless|whether|would|could|may|might|should|must|will|"
                         r"pending|subject to|conditional (?:on|upon)|once|provided that|"
                         r"requires? (?:credit|internal) approval|expect(?:s|ed)?|intend(?:s|ed)?|"
                         r"propos(?:e|ed|al)|plan(?:s|ned)?|to be|upon receipt)\b", re.I)
_QUALIFIER = re.compile(r"\b(?:senior|junior|revolving|subordinated|secured|unsecured|term[- ]loan)\b", re.I)
_CASE = re.compile(r"\b(?:case|scenario|option)(?:\s+([a-z])|\s*(\d+))\b",re.I)
_CONDITIONS = {"credit approval": r"credit approval", "internal approval": r"internal approval",
               "definitive documentation": r"definitive documentation"}
_MONTH_NAMES = {"jan","feb","mar","apr","may","jun","jul","aug","sep","sept","oct","nov","dec",
                "january","february","march","april","june","july","august","september","october","november","december"}
_TOPIC = re.compile(r"\b(?:invitations?|syndication|participation|tranche|term|loans?|commitments?|"
                    r"facilit(?:y|ies)|letters?|lenders?|refinancing)\b", re.I)


def _state(clause, occurrence, kind):
    # The negator must govern this predicate. A prior action of the same
    # family, separated by "and", must not donate its negation.
    before = clause[:occurrence.start()]
    before = re.split(r"\band\b", before, flags=re.I)[-1]
    unconfirmed = bool(re.search(r"\b(?:no (?:evidence|confirmation)|without (?:any )?(?:evidence|confirmation)|"
                                 r"not (?:known|confirmed|established))\b",before,re.I))
    negative = bool(_NEGATED.search(before)) and not unconfirmed
    if kind == "withdrawal" and re.search(r"\bno\s+(?:prior\s+|formal\s+)?$",before,re.I):
        negative = True
    # A calendar month is removed only while deciding contingency: "on
    # 3 May 2026" dates a completed action; modal "may" changes its status.
    clean = re.sub(r"\b(?:\d{1,2}\s+May(?:\s+\d{4})?|May\s+\d{1,2}(?:,?\s+\d{4})?)\b","",clause,flags=re.I)
    contingent = bool(_CONTINGENT.search(_CONDITION.sub("",clean) if kind=="conditional" else clean))
    return negative, contingent or unconfirmed


def _identities(text):
    options = {a.casefold() or b for a,b in _CASE.findall(text)}
    qualifiers = {s.casefold().replace("-", " ") for s in _QUALIFIER.findall(text)}
    # Distinct named tranches/lenders cannot certify one another on the basis
    # of a ledger entity supplied by the candidate.
    names = {name.casefold() for name in re.findall(r"\b[A-Z][a-z]{2,}(?:bank|Bank)?\b",text)
             if name.casefold() not in {"the","this","that","if","once","unless","case","option","scenario",
                "signed","approved","subject","draft","historic","historical","no","letter","invitation","any",
                "senior","junior","facility","loan","tranche","credit","internal","commitment","funding","as",
                "execution","notice","project","refinancing","update","condition","conditions","status",
                "financing","current","withdrawal","action","prepared","for","all","only","on","as","dated"}
             and name.casefold() not in _MONTH_NAMES}
    return options, qualifiers, names


def _topics(text):
    return {"facility" if m[0].casefold().startswith("facilit") else m[0].casefold().rstrip("s")
            for m in _TOPIC.finditer(text)}


def _same_subject(native, evidence):
    native_scopes = _identities(native)
    evidence_scopes = _identities(evidence)
    if native_scopes[:2] != evidence_scopes[:2]:
        return False
    return (all(requested <= actual for requested,actual in zip(native_scopes,evidence_scopes))
            and (not _topics(native) or bool(_topics(native) & _topics(evidence))))


def witnesses_action(native, original, kind):
    """Every predicate requires its own local subject, modality and dated original."""
    pattern = PREDICATES[kind]
    clauses = [s.strip() for s in _CLAUSES.split(original) if s.strip()]
    matched = False
    for clause in _CLAUSES.split(native):
        for occurrence in pattern.finditer(clause):
            matched = True
            state = _state(clause, occurrence, kind)
            required = {s for s,p in _CONDITIONS.items() if re.search(p,clause,re.I)}
            matching_states = set()
            for source in clauses:
                if (not _same_subject(clause,source) or not dates(clause) <= dates(source)
                        or not all(re.search(_CONDITIONS[name],source,re.I) for name in required)):
                    continue
                for verb in pattern.finditer(source):
                    if kind != "execution" or verb[0].casefold() == occurrence[0].casefold():
                        matching_states.add(_state(source,verb,kind))
            if matching_states != {state}:
                return False
    return matched


def completed_withdrawal(quote, entity, field):
    """Retiring an existing fact needs an affirmed completed action on that fact."""
    pattern = PREDICATES["withdrawal"]
    states = set()
    for clause in _CLAUSES.split(quote):
        if not all(x and re.search(r"(?<![a-z0-9])"+re.escape(str(x).strip())+r"(?![a-z0-9])",
                                   clause,re.I|re.ASCII) for x in (entity,field)):
            continue
        if re.search(r"未(?:被|予以)?撤销|未作废|尚未|拟(?:予以)?撤销|可能|可以|如果|若|拟将|待.*(?:作废|撤销)",clause):
            states.add((True, False))
            continue
        if re.search(r"已撤销|予以撤销|作废",clause):
            states.add((False, False))
        for action in pattern.finditer(clause):
            if action[0].casefold() in {"withdrawn","withdrew","revoked","rescinded","cancelled","canceled"}:
                states.add(_state(clause,action,"withdrawal"))
    return states == {(False,False)}
