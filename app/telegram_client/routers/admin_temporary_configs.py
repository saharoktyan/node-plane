"""Administrator-only temporary access; all mutations belong to the backend worker."""
import asyncio
from uuid import uuid4
from urllib.parse import quote

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, BufferedInputFile

from ..backend import BackendClient, BackendError
from ..i18n import normalize_locale, tr
from ..screens import Screen
from .callbacks import AdminSettingsCallback
from .common import render

router = Router()
ROOT = '/api/v1/system/temporary-configs'


def button(locale, key, action, style=None):
    return InlineKeyboardButton(text=tr(locale,key),callback_data='temporary:'+action,style=style)


async def request(backend, user, method, suffix='', **kwargs):
    return await backend.request(method,ROOT+suffix,telegram_user_id=user,**kwargs)


async def draw(query, bot, state, title, lines, rows, **kwargs):
    locale = normalize_locale((await state.get_data()).get('locale'))
    return await render(bot,query.message.chat.id,
        Screen(tr(locale,title),tuple(lines),embedded_buttons=True,navigation=True,**kwargs),
        rows,state,query.message.message_id)


async def catalog(query, bot, backend, state, page=0):
    locale = normalize_locale((await state.get_data()).get('locale'))
    value = await request(backend,query.from_user.id,'GET',f'?page={page}&page_size=8')
    await state.update_data(temporary_page=page)
    rows = [[button(locale,'temporary.create','new','primary')]]
    for item in value['items']:
        label = f"{item.get('node_title',item['node_key'])} · {'VLESS' if item['protocol']=='xray' else 'AWG'} · {tr(locale,'temporary.status.'+item['status'])}"
        if item.get('expires_at'):
            label += ' · '+item['expires_at'][5:16].replace('T',' ')+' UTC'
        rows.append([InlineKeyboardButton(text=label,callback_data='temporary:card:'+item['id'])])
    nav = []
    if page:
        nav.append(InlineKeyboardButton(text='←',callback_data=f'temporary:list:{page-1}'))
    if (page+1)*8 < value['total']:
        nav.append(InlineKeyboardButton(text='→',callback_data=f'temporary:list:{page+1}'))
    if nav:
        rows.append(nav)
    rows.extend([[button(locale,'devices.refresh',f'list:{page}')],
        [InlineKeyboardButton(text=tr(locale,'back'),callback_data=AdminSettingsCallback().pack())]])
    await draw(query,bot,state,'temporary.title',[] if value['items'] else [tr(locale,'temporary.empty')],rows)


async def wizard(query, bot, state, step):
    data = await state.get_data()
    locale = normalize_locale(data.get('locale'))
    draft = data['temporary_draft']
    node = draft.get('node')
    rows, lines = [], []
    if step=='server':
        page = data.get('temporary_node_page',0)
        items = data['temporary_nodes']
        for index,item in enumerate(items[page*8:page*8+8],page*8):
            rows.append([InlineKeyboardButton(text=f"{item.get('flag','')} {item['title']} · {item['region']}",callback_data=f'temporary:node:{index}')])
        nav = []
        if page:
            nav.append(InlineKeyboardButton(text='←',callback_data=f'temporary:servers:{page-1}'))
        if (page+1)*8<len(items):
            nav.append(InlineKeyboardButton(text='→',callback_data=f'temporary:servers:{page+1}'))
        if nav:
            rows.append(nav)
        if not items:
            lines.append(tr(locale,'temporary.no_servers'))
        back = 'list:0'
    elif step=='protocol':
        rows = [[InlineKeyboardButton(text='VLESS' if p=='xray' else 'AmneziaWG',callback_data='temporary:protocol:'+p)] for p in node['protocols']]
        back = 'server'
    elif step=='transport':
        rows = [[InlineKeyboardButton(text=p.upper(),callback_data='temporary:transport:'+p)] for p in node['xray_transports']]
        back = 'protocol' if len(node['protocols'])>1 else 'server'
    elif step=='duration':
        rows = [[InlineKeyboardButton(text=label,callback_data=f'temporary:duration:{seconds}',style='primary')] for seconds,label in ((43200,'12h'),(86400,'1d'),(259200,'3d'))]
        back = 'transport' if draft['protocol']=='xray' and len(node['xray_transports'])>1 else ('protocol' if len(node['protocols'])>1 else 'server')
    else:
        lines = [f"{node['title']} · {'VLESS '+draft['transport'].upper() if draft['protocol']=='xray' else 'AmneziaWG'}", {43200:'12h',86400:'1d',259200:'3d'}[draft['duration_seconds']]]
        if draft['protocol']=='xray':
            lines.append(tr(locale,'temporary.vless_notice'))
        rows = [[button(locale,'temporary.create','issue','primary')]]
        back = 'duration'
    rows.append([button(locale,'back',back)])
    await state.update_data(temporary_step=step)
    await draw(query,bot,state,'temporary.'+step,lines,rows)


async def card(query, bot, backend, state, identity, show=False):
    locale = normalize_locale((await state.get_data()).get('locale'))
    item = await request(backend,query.from_user.id,'GET','/'+identity)
    rows, lines = [], [f"{item.get('node_title',item['node_key'])} · {'VLESS' if item['protocol']=='xray' else 'AmneziaWG'} · {item['transport'].upper()}", tr(locale,'temporary.status.'+item['status'])]
    if item.get('expires_at'):
        lines.append(tr(locale,'temporary.expires',value=item['expires_at'][:19].replace('T',' ')+' UTC'))
    if item['protocol']=='xray':
        lines.append(tr(locale,'temporary.vless_notice'))
    if item['status']=='active' and not show:
        rows.append([button(locale,'temporary.show','show:'+identity)])
    if item['status'] not in {'expired','revoked','cancelled','revoking'}:
        rows.append([button(locale,'temporary.revoke','confirm_revoke:'+identity,'danger')])
    rows.extend([[button(locale,'devices.refresh','card:'+identity)], [button(locale,'back','list:0')]])
    kwargs = {}
    if show:
        artifact = await request(backend,query.from_user.id,'GET','/'+identity+'/artifact')
        from .user import config_qr, qr_payload
        uri = artifact['content']
        image = await asyncio.to_thread(config_qr, qr_payload(item['protocol'],item['transport'],uri))
        lines.append(tr(locale,'ui.config_intro'))
        kwargs = dict(uri=uri,uri_collapsed=True,uri_title=tr(locale,'ui.config_link'),
            qr=image,qr_title=tr(locale,'ui.config_qr'),
            details_title=tr(locale,'ui.config_help'),
            details_lines=(tr(locale,'config.import_xray' if item['protocol']=='xray' else 'config.import_awg_vpn'),),
            uri_rows=((button(locale,'config.link.send','plain:'+identity),),),
            files=tuple((f['filename'],f['content'].encode()) for f in artifact['files']),files_title=tr(locale,'ui.config_files'))
    rich = await draw(query,bot,state,'temporary.title',lines,rows,**kwargs)
    if show and rich is False:
        for name,content in kwargs['files']:
            await bot.send_document(query.message.chat.id,BufferedInputFile(content,name))
        if kwargs.get('qr'):
            await bot.send_photo(query.message.chat.id,BufferedInputFile(kwargs['qr'],'config.png'))


@router.callback_query(F.data.startswith('temporary:'))
async def temporary_cb(query: CallbackQuery, bot: Bot, backend: BackendClient, state: FSMContext):
    await query.answer()
    await state.set_state(None)
    action = query.data.split(':',1)[1]
    locale = normalize_locale((await state.get_data()).get('locale'))
    try:
        if action.startswith('list:'):
            await catalog(query,bot,backend,state,max(0,int(action.split(':')[1])))
        elif action=='new':
            items, cursor, seen = [], None, set()
            while True:
                path = '/api/v1/nodes?limit=100&order=region'+('&cursor='+quote(cursor,safe='') if cursor else '')
                page = await backend.request('GET',path,telegram_user_id=query.from_user.id)
                items.extend(n for n in page['items'] if n['enabled'] and n['applied_revision']>0 and n['applied_revision']==n['desired_revision'] and n['protocols'])
                cursor = page.get('next_cursor')
                if not cursor:
                    break
                if cursor in seen or len(items)>20000:
                    raise BackendError('invalid_node_pagination',502)
                seen.add(cursor)
            await state.update_data(temporary_nodes=items,temporary_node_page=0,temporary_draft={'command_key':str(uuid4())})
            await wizard(query,bot,state,'server')
        elif action.startswith(('card:','show:')):
            await card(query,bot,backend,state,action.split(':')[1],action.startswith('show:'))
        elif action.startswith('plain:'):
            artifact = await request(backend,query.from_user.id,'GET','/'+action.split(':')[1]+'/artifact')
            await bot.send_message(query.message.chat.id,artifact['content'],parse_mode=None,
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=tr(locale,'setup.close'),callback_data='config_uri_close')]]))
        elif action.startswith('confirm_revoke:'):
            identity = action.split(':')[1]
            await draw(query,bot,state,'temporary.revoke',[tr(locale,'temporary.revoke_confirm')],
                [[button(locale,'temporary.revoke','revoke:'+identity,'danger')],[button(locale,'back','card:'+identity)]])
        elif action.startswith('revoke:'):
            identity = action.split(':')[1]
            await request(backend,query.from_user.id,'POST','/'+identity+'/revoke')
            await card(query,bot,backend,state,identity)
        else:
            data = await state.get_data()
            draft = data.get('temporary_draft')
            if not draft:
                await catalog(query,bot,backend,state)
                return
            step = action
            if action.startswith('servers:'):
                await state.update_data(temporary_node_page=max(0,int(action.split(':')[1])))
                step = 'server'
            elif action.startswith('node:'):
                index = int(action.split(':')[1])
                if not 0<=index<len(data['temporary_nodes']):
                    raise BackendError('invalid_input',422)
                draft.update(node=data['temporary_nodes'][index])
                step = 'protocol'
                if len(draft['node']['protocols'])==1:
                    draft['protocol'] = draft['node']['protocols'][0]
                    step = 'transport'
            elif action.startswith('protocol:'):
                value = action.split(':')[1]
                if value not in draft['node']['protocols']:
                    raise BackendError('invalid_input',422)
                draft['protocol'],step = value,'transport'
            elif action.startswith('transport:'):
                value = action.split(':')[1]
                if value not in draft['node']['xray_transports']:
                    raise BackendError('invalid_input',422)
                draft['transport'],step = value,'duration'
            elif action.startswith('duration:'):
                value = int(action.split(':')[1])
                if value not in {43200,86400,259200}:
                    raise BackendError('invalid_input',422)
                draft['duration_seconds'],step = value,'confirm'
            elif action=='issue':
                value = await request(backend,query.from_user.id,'POST',body={'node_key':draft['node']['key'],
                    **{k:draft[k] for k in ('protocol','transport','duration_seconds')}},command=True,command_key=draft['command_key'])
                await state.update_data(temporary_draft=None)
                await card(query,bot,backend,state,value['id'])
                return
            if step=='transport' and draft.get('protocol')=='awg':
                draft['transport'],step='vpn','duration'
            elif step=='transport' and len(draft['node']['xray_transports'])==1:
                draft['transport'],step=draft['node']['xray_transports'][0],'duration'
            await state.update_data(temporary_draft=draft)
            await wizard(query,bot,state,step)
    except (BackendError,ValueError,KeyError,IndexError):
        await draw(query,bot,state,'temporary.title',[tr(locale,'temporary.error')],
            [[button(locale,'devices.refresh','list:0')],[button(locale,'back','list:0')]])
