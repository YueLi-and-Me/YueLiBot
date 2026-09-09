"""只读统计模型复读与发送前拦截；保留任务书的正文提取和相似度口径。"""

from argparse import ArgumentParser
from pathlib import Path
from typing import Any, Dict, List

import difflib
import json
import re
import sqlite3


def report(db_path: Path, since_at: int) -> Dict[str, Any]:
    """按原始 llm_final 统计，并单列验收窗口及每条拦截的历史消息对照。

    窗口从指定毫秒时间起算；相邻比较仍跨过窗口起点，避免漏掉第一轮对旧历史
    的复读。llm_final 是模型原文，护栏拦截不改变它，也不会被算成实际投递。
    """
    db = sqlite3.connect(db_path.resolve().as_uri() + '?mode=ro', uri=True)
    try:
        rows = db.execute(
            "SELECT at, stream_id, turn_id, payload FROM pipeline_events "
            "WHERE kind='llm_final' ORDER BY at"
        ).fetchall()
        says = []
        for at, sid, tid, payload in rows:
            d = json.loads(payload) if payload else {}
            body = ' '.join(re.findall(r'<say[^>]*>(.*?)</say>', d.get('text') or '', re.S)).strip()
            if body:
                says.append((at, sid, tid, body))
        prev, dups = {}, []
        for at, sid, tid, body in says:
            p = prev.get(sid)
            if p and difflib.SequenceMatcher(None, p[1], body).ratio() >= 0.8:
                dups.append((at, sid, p[0], tid, body[:36]))
            prev[sid] = (tid, body)

        blocked: List[Dict[str, Any]] = []
        for at, sid, tid, payload in db.execute(
            "SELECT at, stream_id, turn_id, payload FROM pipeline_events "
            "WHERE kind='reply_say_blocked' AND at >= ? ORDER BY at, seq", (since_at,),
        ):
            event = json.loads(payload)
            history = db.execute(
                'SELECT content FROM messages WHERE stream_id = ? AND id = ?',
                (sid, event['matchedMessageId']),
            ).fetchone()
            blocked.append({
                'at': at, 'streamId': sid, 'turnId': tid, **event,
                'historyMarkup': history[0] if history else None,
            })
        delivered = db.execute(
            "SELECT COUNT(*) FROM pipeline_events WHERE kind='outbound_delivered' AND at >= ?",
            (since_at,),
        ).fetchone()[0]
        return {
            'sinceAt': since_at,
            'allSays': len(says), 'allDuplicates': len(dups),
            'windowSays': sum(at >= since_at for at, *_ in says),
            'windowDuplicates': [d for d in dups if d[0] >= since_at],
            'blockedCount': len(blocked), 'blocked': blocked,
            'outboundDeliveredEvents': delivered,
            'lastSayAt': says[-1][0] if says else None,
        }
    finally:
        db.close()


if __name__ == '__main__':
    parser = ArgumentParser(description=__doc__)
    parser.add_argument('database', type=Path)
    parser.add_argument('--since-at', type=int, default=0, help='验收窗口起点，Unix 毫秒')
    args = parser.parse_args()
    print(json.dumps(report(args.database, args.since_at), ensure_ascii=False, indent=2))
