"""
Pull stocks, price levels, timeframe and call events out of Telegram posts.

Pure functions with no I/O, so every rule here is covered by offline tests.
Nothing is guessed: a level that isn't written next to a stock stays None and
the call lists it in ``missing``. The raw text always travels with the result,
so the reader (Claude, or the user) can check what the channel actually said.

Flow:
  extract_message()  one post -> segments (stock(s), levels, timeframe, event)
  build_calls()      posts in time order -> calls, each with its update events
"""

import re
from dataclasses import dataclass, field
from typing import Iterable, Optional


# ─── symbol matching ──────────────────────────────────────────────────────────

# Words that are written in capitals in trading chats but are not stock calls.
# Several are real NSE symbols too (e.g. STAR, GOLD-like names), which is why a
# bare capitalised word is only taken as a ticker when it isn't on this list;
# "#STAR", "$STAR" or "NSE:STAR" always count.
STOPWORDS = frozenset("""
A AN AND ANY ARE AS AT BE BY DO FOR GO HE IF IN IS IT ME MY NO OF ON OR SO TO UP US WE
ABOVE ADD ALL ALERT AVG BELOW BEST BIG BOOK BREAKOUT BUY CALL CHART CMP DAY DONE ENTRY
EXIT GET GOOD HIGH HIT HOLD HOT LEVEL LONG LOSS LOW NEAR NEW NOT NOTE NOW ONE PLEASE
PROFIT PUT RANGE RESISTANCE SELL SHORT SL STOP SUPPORT TARGET TERM TGT THE TODAY
TOMORROW TOP UPDATE WEEK WITH YES ZONE MONTH YEAR TRADE TRADES STOCK STOCKS VIEW
NSE BSE NIFTY SENSEX BANKNIFTY FINNIFTY MIDCAP SMALLCAP IPO FII FIIS DII DIIS ATH RSI
EMA SMA DMA MACD VWAP OI PE CE FNO LTP QTY PCR GDP RBI SEBI USA UK EU AI EV IT PSU CPSE
SME MF ETF OK BTST STBT INTRADAY SWING POSITIONAL RS INR CR MCAP VOL TF TP EPS ROE ROCE
PAT EBITDA NPA YOY QOQ FY Q1 Q2 Q3 Q4 T1 T2 T3 T4 GOLD SILVER OIL GAS POWER BANK AUTO
PHARMA METAL REALTY INFRA ENERGY MEDIA FMCG STAR JUST WATCH WAIT SAFE RISK
""".split())

_SUFFIXES = re.compile(r"\b(limited|ltd|the)\b\.?", re.I)
_PUNCT = re.compile(r"[^a-z0-9& ]+")
_PUNCT_1 = re.compile(r"[^a-z0-9& ]")  # one for one, so positions stay aligned
# "#TATAMOTORS", "$TCS", "NSE:BEL", "NSE: BEL"
_PREFIXED = re.compile(r"(?:(?<![\w])[#$]|\bNSE\s*:\s*)([A-Za-z][A-Za-z0-9&\-]{0,19})")
_BARE = re.compile(r"(?<![\w#$&])([A-Z][A-Z0-9&\-]{2,19})(?![\w&])")


def normalize_name(name: str) -> str:
    n = _SUFFIXES.sub(" ", name.lower())
    return " ".join(_PUNCT.sub(" ", n).split())


@dataclass
class SymbolIndex:
    """NSE symbols plus company-name aliases ("tata motors" -> TATAMOTORS)."""

    names: dict[str, str]                      # SYMBOL -> company name
    aliases: dict[str, str] = field(default_factory=dict)  # normalized phrase -> SYMBOL
    max_words: int = 1

    @classmethod
    def build(cls, rows: Iterable[tuple[str, str]], extra_aliases: Optional[dict[str, str]] = None) -> "SymbolIndex":
        names: dict[str, str] = {}
        aliases: dict[str, str] = {}
        for sym, name in rows:
            sym = sym.strip().upper()
            if not sym:
                continue
            names[sym] = name.strip()
            alias = normalize_name(name)
            # A one-word name only counts when it's distinctive enough; "india"
            # or "vision" on their own would match ordinary sentences.
            if alias and (" " in alias or len(alias) >= 6) and alias.upper() not in STOPWORDS:
                aliases.setdefault(alias, sym)
        for phrase, sym in (extra_aliases or {}).items():
            sym = sym.strip().upper()
            if sym in names:
                aliases[normalize_name(phrase)] = sym
        max_words = max((len(a.split()) for a in aliases), default=1)
        return cls(names=names, aliases=aliases, max_words=min(max_words, 8))

    def find(self, text: str) -> list[tuple[str, str]]:
        """Stocks named in ``text``, in order of first appearance, as
        (SYMBOL, matched_by) with matched_by one of tag / symbol / name."""
        hits: list[tuple[int, str, str]] = []
        for m in _PREFIXED.finditer(text):
            sym = m.group(1).upper()
            if sym in self.names:
                hits.append((m.start(), sym, "tag"))
        for m in _BARE.finditer(text):
            sym = m.group(1)
            if sym in self.names and sym not in STOPWORDS:
                hits.append((m.start(), sym, "symbol"))
        words = [(w.start(), w.group(0)) for w in re.finditer(r"[a-z0-9&]+", _PUNCT_1.sub(" ", text.lower()))]
        # Positions in the cleaned text line up with the original: the cleaner
        # replaces characters one for one and lower() keeps lengths for ASCII.
        for i in range(len(words)):
            for n in range(min(self.max_words, len(words) - i), 0, -1):
                phrase = " ".join(w for _, w in words[i:i + n])
                if phrase in self.aliases:
                    hits.append((words[i][0], self.aliases[phrase], "name"))
                    break
        seen: dict[str, str] = {}
        for _, sym, how in sorted(hits):
            seen.setdefault(sym, how)
        return list(seen.items())

    def unknown_tags(self, text: str) -> list[str]:
        """#/$-tagged words that look like tickers but aren't NSE symbols."""
        return sorted({m.group(1).upper() for m in _PREFIXED.finditer(text)
                       if m.group(1).upper() not in self.names and m.group(1).upper() not in STOPWORDS})


# ─── levels, timeframe, event ─────────────────────────────────────────────────

_N = r"(\d{1,6}(?:,\d{3})*(?:\.\d{1,2})?)"
_RS = r"(?:rs\.?|inr|₹)?\s*"
_SEP = r"\s*(?:[:\-=@]|is|at|of|around|near|above|below)?\s*"
_RANGE = rf"{_N}(?:\s*(?:-|–|to|/)\s*{_N})?"

_STOP_RE = re.compile(rf"\b(?:sl|stop\s*-?\s*loss|stoploss|stop)\b(?:\s+(?:hit|triggered))?{_SEP}{_RS}{_N}", re.I)
_TARGET_RE = re.compile(
    rf"\b(?:targets?|tgts?|tgt\s*\d|t\s*\d|tp\s*\d?)\b{_SEP}{_RS}{_N}(?!\s*(?:hit|done|achieved|acheived|met|reached|completed)\b)((?:\s*(?:/|,|-|–|&|and)\s*{_RS}\d{{1,6}}(?:,\d{{3}})*(?:\.\d{{1,2}})?)*)",
    re.I)
_ENTRY_RE = re.compile(
    rf"\b(?:buy(?:ing)?|accumulate|add)\b(?:\s+(?!sl\b|stop\b|targets?\b|tgt\b)[#$]?[A-Za-z][\w&\-]*){{0,3}}?{_SEP}{_RS}{_RANGE}"
    rf"|\b(?:entry|enter|long|cmp|range|zone)\b{_SEP}{_RS}{_RANGE}",
    re.I)
_CMP_RE = re.compile(rf"\b(?:cmp|ltp)\b{_SEP}{_RS}{_N}", re.I)

_TIMEFRAMES = [
    ("intraday", re.compile(r"\bintra\s*-?day\b|\bscalp", re.I)),
    ("btst", re.compile(r"\bbtst\b|\bstbt\b", re.I)),
    ("swing", re.compile(r"\bswing\b|\bshort\s*-?\s*term\b|\b\d{1,2}\s*-?\s*(?:to\s*\d{1,2}\s*)?(?:days?|weeks?)\b", re.I)),
    ("positional", re.compile(r"\bpositional\b|\bmedium\s*-?\s*term\b|\b\d{1,2}\s*-?\s*(?:to\s*\d{1,2}\s*)?months?\b", re.I)),
    ("long_term", re.compile(r"\blong\s*-?\s*term\b|\binvestment\b|\binvest\b|\b\d\s*-?\s*(?:to\s*\d\s*)?years?\b", re.I)),
]

# Most specific first: "SL hit" beats "exit", "target done" beats "buy".
_EVENTS = [
    ("stop_hit", re.compile(r"\b(?:sl|stop\s*-?\s*loss|stoploss)\s*(?:hit|triggered|got hit)\b|\bstopped\s+out\b", re.I)),
    ("target_hit", re.compile(
        r"\b(?:t\s*\d|tgt\s*\d?|target\s*\d?|1st target|2nd target|3rd target|all targets?)\s*(?:hit|done|achieved|acheived|met|reached|completed?)\b"
        r"|🎯", re.I)),
    ("cancel", re.compile(r"\b(?:cancel(?:led|ed)?|avoid|ignore|don'?t\s+buy|do\s+not\s+buy|not\s+valid|invalid(?:ated)?|call\s+off)\b", re.I)),
    ("exit", re.compile(
        r"\b(?:exit(?:ed)?|book(?:ed)?\s+(?:full\s+|partial\s+|some\s+)?profits?|book\s+(?:full|partial)|square\s*-?\s*off|close\s+(?:the\s+)?(?:position|trade)|sell\s+(?:all|now|at|above|near))\b", re.I)),
    ("update", re.compile(r"\btrail(?:ing)?\s*(?:sl|stop)|\b(?:revised?|new|modified?|change)\s+(?:sl|stop|targets?|tgt)\b", re.I)),
]
_SHORT_RE = re.compile(r"\b(?:short|sell\s+below|stbt)\b", re.I)
_BUY_RE = re.compile(r"\b(?:buy|accumulate|add|entry|enter|long|breakout|break\s*out)\b", re.I)
_PARTIAL_RE = re.compile(r"\bpartial|\bhalf\b|\bsome\b", re.I)
_ALL_TARGETS_RE = re.compile(r"\ball\s+targets?\b|\bfinal\s+target\b", re.I)
_TARGET_NO_RE = re.compile(r"\b(?:t|tgt|target)\s*(\d)\s*(?:hit|done|achieved|acheived|met|reached)", re.I)

_BULL = re.compile(r"\b(?:buy|accumulate|breakout|bullish|uptrend|strong|momentum|rally|add|long)\b|🚀|📈", re.I)
_BEAR = re.compile(r"\b(?:sell|exit|avoid|bearish|weak|breakdown|downtrend|short|book)\b|📉", re.I)


def _num(s: Optional[str]) -> Optional[float]:
    if not s:
        return None
    try:
        return float(s.replace(",", ""))
    except ValueError:
        return None


def parse_levels(text: str) -> dict:
    """Entry range, stop and targets written in ``text``. Unwritten -> None / []."""
    out: dict = {"entry_low": None, "entry_high": None, "stop": None, "targets": [], "cmp": None}
    if m := _STOP_RE.search(text):
        out["stop"] = _num(m.group(1))
    targets: list[float] = []
    for m in _TARGET_RE.finditer(text):
        targets.append(_num(m.group(1)))
        targets += [_num(x) for x in re.findall(r"\d{1,6}(?:,\d{3})*(?:\.\d{1,2})?", m.group(2) or "")]
    out["targets"] = sorted({t for t in targets if t is not None and t != out["stop"]})
    for m in _ENTRY_RE.finditer(text):
        lo, hi = _num(m.group(1) or m.group(3)), _num(m.group(2) or m.group(4))
        if lo is None or lo in out["targets"] or lo == out["stop"]:
            continue
        if hi is not None and hi < lo:
            lo, hi = hi, lo
        out["entry_low"], out["entry_high"] = lo, hi if hi is not None else lo
        break
    if m := _CMP_RE.search(text):
        out["cmp"] = _num(m.group(1))
    return out


def parse_timeframe(text: str) -> Optional[str]:
    for name, rx in _TIMEFRAMES:
        if rx.search(text):
            return name
    return None


def classify_event(text: str, levels: dict) -> str:
    """entry / target_hit / stop_hit / exit / cancel / update / mention."""
    for name, rx in _EVENTS:
        if rx.search(text):
            if name == "cancel" and _BUY_RE.search(text) and levels["stop"] is not None and levels["entry_low"] is not None:
                break  # "avoid chasing, buy at 450 SL 430" is a fresh call
            return name
    has_levels = levels["stop"] is not None or bool(levels["targets"]) or levels["entry_low"] is not None
    if (_BUY_RE.search(text) or _SHORT_RE.search(text)) and has_levels:
        return "entry"
    if levels["stop"] is not None and (levels["entry_low"] is not None or levels["targets"]):
        return "entry"
    return "mention"


def direction(text: str, levels: dict) -> str:
    """long / short / unknown, from the words and, failing that, the level order."""
    entry, stop, targets = levels["entry_low"], levels["stop"], levels["targets"]
    ref = entry if entry is not None else levels.get("cmp")
    if _SHORT_RE.search(text):
        return "short"
    if ref is not None and stop is not None:
        if stop < ref and (not targets or min(targets) > ref):
            return "long"
        if stop > ref and (not targets or max(targets) < ref):
            return "short"
    if stop is not None and targets:
        return "long" if min(targets) > stop else "short"
    return "long" if _BUY_RE.search(text) else "unknown"


def tone(text: str) -> str:
    bull, bear = len(_BULL.findall(text)), len(_BEAR.findall(text))
    return "bullish" if bull > bear else "bearish" if bear > bull else "neutral"


# ─── one message ──────────────────────────────────────────────────────────────

@dataclass
class Segment:
    symbols: list[str]
    matched_by: dict[str, str]
    text: str
    event: str
    levels: dict
    timeframe: Optional[str]
    direction: str
    tone: str
    levels_ambiguous: bool = False  # several stocks on one line: levels not assigned


def _split_by_stock(text: str, index: SymbolIndex) -> list[tuple[list[tuple[str, str]], str]]:
    """Break a post into (stocks, lines) chunks. A line that names a stock starts
    a chunk; lines after it, up to the next stock, belong to it."""
    chunks: list[tuple[list[tuple[str, str]], list[str]]] = []
    preamble: list[str] = []
    for line in text.splitlines():
        found = index.find(line)
        if found:
            chunks.append((found, [line]))
        elif chunks:
            chunks[-1][1].append(line)
        else:
            preamble.append(line)
    out = []
    for found, lines in chunks:
        out.append((found, "\n".join(lines)))
    return out if out else [([], "\n".join(preamble))]


def extract_message(text: str, index: SymbolIndex, inherited: Optional[list[str]] = None) -> list[Segment]:
    """Segments of one post. ``inherited`` = stocks of the post this one replies
    to, used when a reply such as "Target 1 done" names no stock itself."""
    text = text or ""
    whole = index.find(text)
    if len({s for s, _ in whole}) <= 1:
        # A reply that names no stock inherits its parent's, if that was one stock.
        found = whole or ([(inherited[0], "reply")] if inherited and len(set(inherited)) == 1 else [])
        pieces = [(found, text)]
    else:
        pieces = _split_by_stock(text, index)
    shared_tf = parse_timeframe(text)
    segs = []
    for found, chunk in pieces:
        if not found:
            continue
        lv = parse_levels(chunk)
        ambiguous = False
        if len(found) > 1:
            # Several stocks in one chunk: say nothing about whose levels these are.
            ambiguous = lv["stop"] is not None or bool(lv["targets"]) or lv["entry_low"] is not None
            lv = {"entry_low": None, "entry_high": None, "stop": None, "targets": [], "cmp": None}
        segs.append(Segment(
            symbols=[s for s, _ in found],
            matched_by=dict(found),
            text=chunk.strip(),
            event=classify_event(chunk, lv),
            levels=lv,
            timeframe=parse_timeframe(chunk) or shared_tf,
            direction=direction(chunk, lv),
            tone=tone(chunk),
            levels_ambiguous=ambiguous,
        ))
    return segs


# ─── calls across messages ────────────────────────────────────────────────────

CLOSED = {"closed_target", "closed_stop", "closed_exit", "cancelled"}


@dataclass
class Post:
    chat_id: int
    msg_id: int
    posted_at: str          # ISO 8601, UTC
    text: str
    topic_id: Optional[int] = None
    reply_to: Optional[int] = None
    edited_at: Optional[str] = None
    deleted: bool = False
    media: Optional[str] = None


def _event_record(post: Post, seg: Segment, symbol: str) -> dict:
    lv = seg.levels
    rec = {
        "type": seg.event,
        "at": post.posted_at,
        "message_id": post.msg_id,
        "text": seg.text,
        "tone": seg.tone,
    }
    if seg.event == "target_hit":
        m = _TARGET_NO_RE.search(seg.text)
        rec["target_no"] = int(m.group(1)) if m else None
        rec["all_targets"] = bool(_ALL_TARGETS_RE.search(seg.text))
    if seg.event == "exit":
        rec["partial"] = bool(_PARTIAL_RE.search(seg.text))
    if lv["stop"] is not None:
        rec["stop"] = lv["stop"]
    if lv["targets"]:
        rec["targets"] = lv["targets"]
    if lv["cmp"] is not None:
        rec["price"] = lv["cmp"]
    if post.edited_at:
        rec["edited_at"] = post.edited_at
    if post.deleted:
        rec["deleted"] = True
    return rec


def _new_call(post: Post, seg: Segment, symbol: str) -> dict:
    lv = seg.levels
    call = {
        "call_id": f"{post.chat_id}:{post.msg_id}:{symbol}",
        "chat_id": post.chat_id,
        "topic_id": post.topic_id,
        "symbol": symbol,
        "matched_by": seg.matched_by.get(symbol),
        "opened_at": post.posted_at,
        "message_id": post.msg_id,
        "direction": seg.direction,
        "timeframe": seg.timeframe,
        "entry": {"low": lv["entry_low"], "high": lv["entry_high"]},
        "price_at_post": lv["cmp"],
        "stop": lv["stop"],
        "targets": lv["targets"],
        "status": "open",
        "text": seg.text,
        "events": [],
    }
    if post.edited_at:
        call["edited_at"] = post.edited_at
    if post.deleted:
        call["deleted"] = True
    call["missing"] = [k for k, v in (("entry", lv["entry_low"]), ("stop", lv["stop"]),
                                      ("targets", lv["targets"] or None), ("timeframe", seg.timeframe)) if v is None]
    return call


def _apply(call: dict, ev: dict) -> None:
    call["events"].append(ev)
    t = ev["type"]
    if call["status"] in CLOSED:
        return  # recorded, but a closed call stays closed
    if t == "stop_hit":
        call["status"] = "closed_stop"
    elif t == "target_hit":
        last = len(call["targets"]) and ev.get("target_no") == len(call["targets"])
        call["status"] = "closed_target" if ev.get("all_targets") or last else "target_hit"
    elif t == "exit" and not ev.get("partial"):
        call["status"] = "closed_exit"
    elif t == "cancel":
        call["status"] = "cancelled"


def build_calls(posts: Iterable[Post], index: SymbolIndex) -> dict:
    """Group posts into calls and their follow-ups.

    A post whose event is ``entry`` opens a call per stock. Later events for the
    same stock in the same chat attach to the call the post replies to, else to
    that stock's latest call. Events with no call to attach to are kept in
    ``unlinked_events`` rather than dropped, and plain discussion goes to
    ``mentions``. Every record keeps its message id and text.
    """
    posts = sorted(posts, key=lambda p: (p.posted_at, p.msg_id))
    by_msg: dict[tuple[int, int], list[str]] = {}     # post -> stocks it named
    calls_by_msg: dict[tuple[int, int, str], dict] = {}
    latest: dict[tuple[int, str], dict] = {}
    calls: list[dict] = []
    unlinked: list[dict] = []
    mentions: list[dict] = []
    image_only = 0

    for post in posts:
        if not (post.text or "").strip():
            image_only += 1 if post.media else 0
            continue
        parent = by_msg.get((post.chat_id, post.reply_to)) if post.reply_to else None
        segs = extract_message(post.text, index, inherited=parent)
        by_msg[(post.chat_id, post.msg_id)] = [s for seg in segs for s in seg.symbols]
        for seg in segs:
            for symbol in seg.symbols:
                key = (post.chat_id, symbol)
                if seg.event == "entry" and not seg.levels_ambiguous:
                    call = _new_call(post, seg, symbol)
                    calls.append(call)
                    calls_by_msg[(post.chat_id, post.msg_id, symbol)] = call
                    latest[key] = call
                    continue
                ev = _event_record(post, seg, symbol)
                if seg.event in ("mention", "entry"):
                    ev["symbol"] = symbol
                    ev["chat_id"] = post.chat_id
                    ev["topic_id"] = post.topic_id
                    if seg.levels_ambiguous:
                        ev["note"] = "Several stocks on one line; levels not assigned to any."
                    mentions.append(ev)
                    continue
                target = calls_by_msg.get((post.chat_id, post.reply_to, symbol)) if post.reply_to else None
                target = target or latest.get(key)
                if target is None:
                    ev.update(symbol=symbol, chat_id=post.chat_id, topic_id=post.topic_id)
                    unlinked.append(ev)
                else:
                    _apply(target, ev)
    return {"calls": calls, "unlinked_events": unlinked, "mentions": mentions, "image_only_posts": image_only}
