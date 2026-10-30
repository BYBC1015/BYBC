"""Read-only, bounded production plans selected from real task/conversation input.

These are execution contracts, not evidence that assets were read or approved.
No jobs, approvals, file bindings or execution leases are created here.
"""
from __future__ import annotations

import re

PRODUCTION_ROLES = {'controller', 'narrative_direction', 'art_direction',
                    'visual_production', 'postproduction', 'reference_research'}
_FOLLOWUP = re.compile(r'继续|接着|接续|上一版|这一版|这版|下一[集幕场]|第.{0,4}[集幕场]|'
                       r'机位|多角度|侧面|正面|母版|同一场景|分镜|剧本|绿幕|抠像|去人物|清理背景|返工|审核', re.I)
_INFRA = re.compile(r'(?:hub|网页|网站|页面|界面|UI|客户端|安卓|网络|软件|接口|MCP|API|登录|上传|下载).{0,16}(?:bug|故障|配置|代码|功能|卡顿|修复|旧版)|'
                    r'(?:修复|排查|检查|维护|升级|配置|开发).{0,16}(?:hub|网页|网站|页面|界面|UI|客户端|安卓|网络|软件|接口|MCP|API|登录|上传|下载)', re.I)
_CANCEL = re.compile(r'(?:不做|不要做|取消|暂停).{0,8}(?:短剧|微短剧|舞剧|舞台背景|舞美)|换个话题|另一个项目', re.I)


def _kinds(text):
    if _INFRA.search(text):
        return []
    result = []
    if re.search(r'短剧|微短剧|竖屏剧|short[- ]form drama', text, re.I) and re.search(
            r'制作|创作|拍摄|剧本|分集|分镜|镜头|生成|规划|流程|production|storyboard', text, re.I):
        if not re.search(r'(?:不做|不要做|取消|暂停)[^，,。；;\n]{0,8}(?:短剧|微短剧|竖屏剧)', text):
            result.append('short_drama')
    if re.search(r'舞剧|舞台剧|舞台背景|舞美|dance drama|stage backdrop', text, re.I) and re.search(
            r'背景|静帧|场景|分幕|合成|绿幕|制作|生成|审核|backdrop|production', text, re.I):
        if not re.search(r'(?:不做|不要做|取消|暂停)[^，,。；;\n]{0,8}(?:舞剧|舞台剧|舞台背景|舞美)', text):
            result.append('stage_background')
    return result


def match_workflows(task, brief):
    current = '\n'.join(str(task.get(key) or '') for key in ('title', 'description'))[:20000]
    if _INFRA.search(current):
        return [], None
    kinds = _kinds(current)
    if kinds:
        return kinds, {'source': 'current_task', 'task_id': task.get('id')}
    if _CANCEL.search(current):
        return [], None
    if not _FOLLOWUP.search(current):
        return [], None
    sources = [brief.get('initial_requirement') or {}, *(brief.get('user_updates') or [])]
    for source in reversed(sources):
        text = str(source.get('user_request') or '')[:2000]
        # A recent explicit change of subject defeats an old production anchor.
        if _INFRA.search(text):
            return [], None
        kinds = _kinds(text)
        if kinds:
            return kinds, {'source': 'conversation_user_requirement', 'task_id': source.get('task_id')}
        if _CANCEL.search(text):
            return [], None
    return [], None


BLUEPRINTS = {
    'short_drama': {
        'name': '短剧制作',
        'stages': [
            {'id':'story', 'owner':'narrative_direction', 'needs':[],
             'deliver':['分集/分场剧本、角色关系与人物连续性表', '镜头清单：场次/镜头编号、动作、台词、时长、景别、机位与转场',
                        '明确制作brief与每个镜头验收要求；建议和已确认要求分开']},
            {'id':'look', 'owner':'art_direction', 'needs':['story'],
             'deliver':['角色/场景审美标尺、母版制作要求与允许变化边界', '关键镜头和多视角连续性检查标准；此阶段不声称已经评议未制作的镜头']},
            {'id':'shots', 'owner':'visual_production', 'needs':['story','look'],
             'deliver':['对应镜头编号的角色/场景/画面源素材与可预览版本', '逐镜头来源母版ID、版本、哈希、生成工具与实际自检结果']},
            {'id':'keyshot_review', 'owner':'art_direction', 'needs':['shots'],
             'deliver':['实际审看关键镜头及同场景多视角，按当前母版ID/版本/哈希记录连续性评议与问题', '先修正不符合项，未知项明确说明，不能以文件存在冒充完成审核']},
            {'id':'still_cleanup', 'owner':'visual_production', 'needs':['keyshot_review'],
             'when':'仅当请求已有静态图去人物/清理/抠像/绿幕合成时',
             'deliver':['保留已审母版构图与空间，按请求范围交付静态派生图、源图与改动区域记录']},
            {'id':'finish', 'owner':'postproduction', 'needs':['keyshot_review'],
             'conditional_needs':{'still_cleanup':'本轮要求静态清理时，消费本轮已完成派生图；无此需求不增加该阶段'},
             'deliver':['时间线、合成/抠像/清理、字幕与声音、调色及成片', '镜头顺序、轴线、角色状态、字幕同步和输出规格的实际检查记录']},
        ],
    },
    'stage_background': {
        'name': '舞剧分幕静帧背景与合成',
        'stages': [
            {'id':'acts', 'owner':'narrative_direction', 'needs':[],
             'deliver':['分幕情绪/剧情与背景功能表、演员走位及留白需求', '按场景和机位编号的静帧清单，明确哪些视角属于同一空间']},
            {'id':'master', 'owner':'visual_production', 'needs':['acts'],
             'deliver':['优先复用用户指定母版；从零创作时按授权和风格要求制作单场景母版', '母版实际附件与版本记录，不提前各自生成互不一致的角度']},
            {'id':'anchor', 'owner':'art_direction', 'needs':['master'],
             'deliver':['实际审看母版，记录空间结构与舞台画幅标尺；保留项/可变项分开', '评议绑定具体母版附件ID/版本/哈希与差异证据，不用风格词代替空间关系']},
            {'id':'views', 'owner':'visual_production', 'needs':['acts','anchor'],
             'deliver':['按已审母版制作每幕同场景多机位背景与分层源文件（按实际能力交付）',
                        '每张图的场景ID、母版版本/哈希、机位参数、输入与输出附件编号']},
            {'id':'view_audit', 'owner':'art_direction', 'needs':['views'],
             'deliver':['标注同一场景母版与各角度的对照联系表', '逐项多视角一致性结果：通过/不通过/无法判断，问题区域和对应附件证据；不符先修正']},
            {'id':'still_cleanup', 'owner':'visual_production', 'needs':['view_audit'],
             'when':'仅当本轮要求静态图去人物/净底/抠像/绿幕合成时',
             'deliver':['在已核对的静态母版上按请求去人物或清理/合成，保留原构图与空间结构',
                        '交付派生图、对应原图与改动区域记录；不无请求重新设计场景，必要遮挡补绘单独说明']},
            {'id':'composite', 'owner':'postproduction', 'needs':['view_audit'],
             'conditional_needs':{'still_cleanup':'本轮要求静态清理时，消费本轮已完成净底；无此需求不增加该阶段'},
             'when':'仅当本轮要求现有视频/时间线合成时；静态单图修图归视觉制作',
             'deliver':['按原始视频机位完成绿幕抠像/边缘去溢色/跟踪遮挡与光影合成，清理指定人物或背景',
                        '保留原始素材与净底版本，导出对应分幕文件、工程和可预览结果；用实际工具执行']},
        ],
    },
}

ANCHOR_FIELDS = ['scene_id', 'anchor_revision', 'source_attachment_id', 'source_sha256',
                 'source_task_id', 'artifact_generation', 'selection_or_review_evidence_source',
                 'fixed_geometry_landmarks_and_props', 'camera_position_height_yaw_pitch_fov',
                 'horizon_ground_plane_scale', 'lighting_direction_material_palette',
                 'stage_aspect_resolution_actor_safe_area', 'allowed_changes', 'forbidden_changes']

MULTIVIEW_CHECKS = [
    '母版ID/版本/哈希与所有视角绑定一致；参考仍可读取；新版修正不误用旧母版',
    '门窗/柱/台阶/主道具的位置、数量、尺度与拓扑关系相同；左右关系不得凭风格相似冒充一致',
    '机位转动/位移、镜头视角、地平线、消失点和遮挡变化符合记录的同一空间；未见面不能任意发明结构',
    '材质、光源方向、时间/色调、地面与演员尺度/站位保持连续；角色留白和实际舞台画幅成立',
    '实际打开母版与各图并列或叠加对照，附附件ID/哈希、差异区域与修正结论；未打开或看不清填无法判断',
    '哈希只能绑定文件版本，不能证明美学或几何一致；母版版本/哈希改变后旧评议失效，检查受影响衍生图并重新审核，不能按相同标题复用通过结论',
    '仅有2D图不能保证严格三维一致；要求精确多机位时先取得或建立同一3D/几何场景，再复用相机，不能声称已重建',
]


def workflow_blueprint(task, agent, brief, inputs):
    role = 'controller' if agent.get('kind') == 'controller' else agent.get('role_key')
    if role not in PRODUCTION_ROLES or task.get('controller_read_only') or task.get('work_stage') == 'delivery_summary':
        return None
    kinds, matched = match_workflows(task, brief)
    if not kinds:
        return None
    workflows = []
    current = str(task.get('title') or '') + '\n' + str(task.get('description') or '')
    derivative_only = bool(re.search(r'已有|现有|这张|原图|这段|原视频|(?:使用|依据|沿用).{0,8}母版', current) and
                           re.search(r'去人物|清理|抠像|绿幕|合成|擦除|跟踪', current))
    has_video = bool(re.search(r'视频|时间线|跟踪|video|timeline', current, re.I))
    operation_owner = 'postproduction' if has_video else 'visual_production'
    for kind in kinds:
        blueprint = BLUEPRINTS[kind]
        stages = blueprint['stages']
        if derivative_only:
            stages = [dict(stage, needs=[], source_requirements=[
                '实际读取本次既有源图/视频及最新修正，核对附件ID/哈希，不能仅凭文件名或旧摘要',
                '保留既有构图、空间和已定版内容，只处理请求范围，不重走创作流程'])
                for stage in stages if stage['id'] in ({'composite','finish'} if has_video else {'still_cleanup'})]
        if role not in {'controller', 'reference_research'}:
            stages = [stage for stage in stages if stage['owner'] == role]
        workflows.append({'key':kind, 'name':blueprint['name'],
                          'stages':[dict(stage, deliver=list(stage['deliver']), needs=list(stage['needs'])) for stage in stages]})
    sources = []
    for item in (inputs.get('attachments') or [])[:12]:
        sources.append({key:item.get(key) for key in ('id','original_filename','sha256','source_task_id','artifact_generation')})
    omitted = [{key:item.get(key) for key in ('id','source_task_id','required_unloaded')} for item in (inputs.get('reference_files') or [])[:12]]
    result = {
        'schema_version':1, 'matched_requirement':matched, 'workflows':workflows,
        'mode':'source_preserving_derivative' if derivative_only else 'production_plan',
        'status':'planning_contract_not_completed_review',
        'input_evidence':{'bound_manifest':sources, 'unloaded_references':omitted,
                          'reference_basis':'user_selected_or_authorized_internal_master_review',
                          'visual_comparison':'not_performed_by_this_context_builder'},
        'coordination':[
            '先检查同对话work_assignments与最新用户修正；按现有work_key接续或revise，不自动创建重复任务。',
            '仅安排当前需要的最小交付；同一场景母版先固定，独立场景可并行，同一文件按版本顺序交接。',
            '用户明确指定参考/修正优先；依赖制作前实际读取当前有效参考，记录附件ID+哈希+版本和用户选择或内部评议证据。上传时间晚、任务done或文件名final都不代表已核验。',
            '从零创作没有既定母版时，按任务授权先由编导/美术明确标准、视觉制作母版、美术实际审看后内部定版；不额外索要用户逐阶段批准。',
            '资料关系不明、已指定参考未载入、哈希/版本不符或一致性未通过，只处理不依赖它的部分并合并说明具体缺口；不先派依赖这些资料的下游反复返工。',
            '用户新修正优先，已有输入manifest和正在执行的版本不改写；通过revise形成可追踪接续，不能静默换参考。',
            '以上是执行标准，不是已完成的审核证据；执行者自检，美术负责关键方向/多角度审核，总控汇总实际成果，不增加逐项人工审批。',
            '现有静态图清理/去人物/抠像归视觉制作；现有视频跟踪/抠像/合成归后期。仅在请求范围内派生，保留原构图、空间、人物设定及已定版内容，必要补绘说明范围；不得无请求重生成整场景。',
        ],
    }
    if derivative_only:
        result['current_operation_owner'] = operation_owner
        result['scope_note'] = '复用既有成果只处理本次改动，不重走全部创作阶段；若本任务归属不符，向总控说明，不自行抢活或另建任务。'
    if role in {'controller','narrative_direction','art_direction','visual_production'}:
        result['scene_anchor_record_fields'] = list(ANCHOR_FIELDS)
    multiview_requested = bool(re.search(r'多角度|多视角|不同角度|多机位|同场景|同一场景|[两三四2-9]视角|角度一致|视角一致', current))
    if role in {'controller','art_direction','visual_production','postproduction'} and (not derivative_only or multiview_requested):
        result['multiview_audit'] = {'required_for_same_scene_multiple_views':True,
            'owner':'art_direction', 'checks':list(MULTIVIEW_CHECKS),
            'record_fields':['scene_id','anchor_revision','source_attachment_ids_and_sha256',
                             'view_labels','criterion','pass_fail_or_unknown','evidence_region','fix_or_unverified_reason']}
    if role == 'reference_research':
        result['research_scope'] = '只检索当前题材/舞台空间/镜头语言所需参考；优先一年内有可核验日期的案例，保留来源，不代替已确认母版或自行决定风格。'
    return result
