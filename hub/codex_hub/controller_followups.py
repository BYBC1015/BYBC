"""Conservative, server-owned recognition of read-only controller follow-ups.

This deliberately does not classify arbitrary instructions with an LLM. Only a
complete, short status/materials question receives the concurrent read lane.
"""
import re
import unicodedata


_QUESTIONS = re.compile(
    r'(?:现在|目前)?(?:进度|状态)(?:怎么样|如何|到哪(?:一步)?了|呢|怎样了)(?:了)?'
    r'|(?:现在|目前)?(?:怎么样了|如何了|做好了吗|完成了吗|做完了吗|处理好了吗|有进展吗)'
    r'|(?:你|您|现在|目前)?(?:还)?(?:需要|缺少|缺)(?:我)?(?:提供|补充)?'
    r'(?:什么|哪些)(?:资料|材料|信息|东西)?(?:吗|呢)?'
    r'|(?:你|您|现在|目前)?还需要(?:其他|其它|更多)(?:资料|材料|信息)(?:吗|呢)?'
    r'|(?:我)?还(?:要|需要)(?:提供|补充)(?:什么|哪些)(?:资料|材料|信息)?(?:吗|呢)?'
    r'|(?:你能|能|可以|请)?(?:解释|说明)(?:一下)?(?:刚才|当前|现在)的安排(?:吗|呢)?'
)


def is_read_only_followup(text):
    if not isinstance(text, str) or not 1 <= len(text) <= 80:
        return False
    normalized = unicodedata.normalize('NFKC', text).strip()
    # Remove only spacing and trailing question punctuation; extra sentences,
    # commands or attached references do not become read-only accidentally.
    normalized = re.sub(r'\s+', '', normalized).rstrip('?？。!！')
    return bool(_QUESTIONS.fullmatch(normalized))


def allows_read_only_followup(*, agent, route, description, attachments, user_id, source_task_id):
    return bool(agent.get('kind') == 'controller' and route == 'chat'
                and user_id is not None and source_task_id is None
                and not attachments and is_read_only_followup(description))
