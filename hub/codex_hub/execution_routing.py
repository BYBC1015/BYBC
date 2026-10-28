"""Choose a work surface without conflating identity, model or resource scope."""
from __future__ import annotations


EXECUTION_ROUTES = frozenset({'chat', 'direct', 'project'})
PROJECT_WORK_TYPES = frozenset({'engineering', 'postproduction'})


def validate_route_request(route=None, reason=''):
    if route is not None and (not isinstance(route, str) or route not in EXECUTION_ROUTES):
        raise ValueError('execution_route 只能是 chat、direct 或 project')
    if not isinstance(reason, str) or len(reason) > 500:
        raise ValueError('execution_route_reason 必须是最多 500 字的文本')
    return route, reason.strip()


def choose_execution_route(agent, *, work_type=None, route=None, reason='',
                           delegated=False, execution_mode='shared'):
    """Freeze one explicit/default route; never infer profession from free text."""
    route, reason = validate_route_request(route, reason)
    if route is None:
        if agent['kind'] == 'controller' and not delegated:
            route = 'chat'
            reason = reason or '总控先回应和协调；实际交付按需单独安排。'
        elif (work_type or agent.get('role_key')) in PROJECT_WORK_TYPES:
            route = 'project'
            reason = reason or '持续本地工程或后期工作，在项目工作会话中接续。'
        else:
            route = 'direct'
            reason = reason or '本次交付直接使用工具或 CLI 完成，保留成果与工作记录。'
    if delegated and route == 'chat':
        raise ValueError('委派交付不能使用 chat；咨询由总控直接回应，实际工作请选择 direct 或 project')
    if route == 'project' and execution_mode == 'isolated':
        raise ValueError('project 需要稳定项目目录，不能使用每次任务独立的 isolated 目录')
    return route, reason or {'chat': '发起者指定直接交流。',
                             'direct': '发起者指定本次直接完成。',
                             'project': '发起者指定在本地项目工作会话中持续制作。'}[route]
