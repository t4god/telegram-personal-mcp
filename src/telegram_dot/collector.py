"""Passive capture plus durable, independent Telegram difference checkpoints."""
import asyncio
import hashlib
import time
from datetime import datetime,timezone
from telethon import functions,types,utils,errors
from .event_queue import canonical


def iso(value):
    return value.isoformat() if value else None


def attachment_metadata(m):
    file=getattr(m,'file',None)
    if not file:return []
    return [dict(index=0,name=getattr(file,'name',None),mime_type=getattr(file,'mime_type',None),
        size=getattr(file,'size',None),duration=getattr(file,'duration',None),
        width=getattr(file,'width',None),height=getattr(file,'height',None),
        voice=bool(getattr(m,'voice',None)),photo=bool(getattr(m,'photo',None)),downloaded=False)]


def message_payload(m):
    reply=getattr(m,'reply_to',None)
    return dict(chat_id=str(utils.get_peer_id(m.peer_id)),message_id=m.id,
        sender_id=str(utils.get_peer_id(m.from_id)) if getattr(m,'from_id',None) else None,
        text=getattr(m,'message','') or '',date_utc=iso(getattr(m,'date',None)),
        edit_date_utc=iso(getattr(m,'edit_date',None)),outgoing=bool(getattr(m,'out',False)),
        mentioned=bool(getattr(m,'mentioned',False)),attachments=attachment_metadata(m),
        reply=dict(message_id=getattr(reply,'reply_to_msg_id',None),
                   topic_id=getattr(reply,'reply_to_top_id',None),forum_topic=bool(getattr(reply,'forum_topic',False)),
                   peer_id=str(utils.get_peer_id(reply.reply_to_peer_id)) if getattr(reply,'reply_to_peer_id',None) else None),
        service_message=bool(getattr(m,'action',None)))


class Collector:
    def __init__(self,client,queue):
        self.client,self.queue=client,queue
        self.pending_channels=set()
        self.active=False
        self.last_success=None
        self.error=None
        self.retry_at=0
        self.lock=asyncio.Lock()
        self.task=None
        self.events_since_start=0
        self.last_event=None

    def record(self,events,checkpoint=None):
        inserted=self.queue.append(events,checkpoint)
        self.events_since_start+=len(inserted)
        if inserted:self.last_event=time.time()
        return inserted

    def make_event(self,m,kind):
        p=message_payload(m)
        if kind=='message.created':key='create:'+p['chat_id']+':'+str(m.id)
        else:key='edit:'+p['chat_id']+':'+str(m.id)+':'+hashlib.sha256(canonical(p).encode()).hexdigest()
        return dict(p,type=kind,dedupe=key,occurred_at=p['edit_date_utc'] if kind.endswith('edited') else p['date_utc'])

    def updates(self,update):
        name=type(update).__name__
        if name in ('UpdateNewMessage','UpdateNewChannelMessage'):
            return [self.make_event(update.message,'message.created')]
        if name in ('UpdateEditMessage','UpdateEditChannelMessage'):
            return [self.make_event(update.message,'message.edited')]
        if name in ('UpdateDeleteMessages','UpdateDeleteChannelMessages'):
            result=[]
            cid=str(utils.get_peer_id(types.PeerChannel(update.channel_id))) if hasattr(update,'channel_id') else None
            for mid in update.messages:
                chat=cid or self.queue.previous_chat(mid)
                result.append(dict(type='message.deleted',dedupe='delete:'+str(cid or 'global')+':'+str(update.pts)+':'+str(mid),
                    occurred_at=None,chat_id=chat,message_id=mid,sender_id=None,text=None,attachments=[],reply=None,
                    limitations=['Telegram deletion update does not include message content or deletion time']+
                                 ([] if chat else ['chat_id unavailable; message was not previously observed']),
                    telegram_pts=getattr(update,'pts',None)))
            return result
        return []

    async def raw(self,update):
        if not self.active:return
        try:
            self.record(self.updates(update))
            channel=getattr(update,'channel_id',None)
            m=getattr(update,'message',None)
            if m and isinstance(getattr(m,'peer_id',None),types.PeerChannel):channel=m.peer_id.channel_id
            if channel:
                key='channel:'+str(channel)
                if self.queue.get_state(key) is None and getattr(update,'pts',None):
                    self.queue.set_state(key,max(1,update.pts-getattr(update,'pts_count',0)))
                    self.queue.gap('channel_baseline_unknown',channel_id=str(channel))
                self.pending_channels.add(channel)
            if type(update).__name__=='UpdatesTooLong':self.retry_at=0
        except Exception as exc:
            self.error=type(exc).__name__
            self.queue.gap('ingest_failed',error_type=self.error)

    async def start(self):
        self.active=True
        # Dialogs populate channel entities without marking anything read.
        dialogs=await self.client.get_dialogs(limit=None)
        for d in dialogs:
            pts=getattr(d.dialog,'pts',None)
            if d.is_channel and pts and self.queue.get_state('channel:'+str(d.entity.id)) is None:
                self.queue.set_state('channel:'+str(d.entity.id),pts)
            if d.is_channel:
                self.pending_channels.add(d.entity.id)
        if self.queue.get_state('global') is None:
            state=await self.client(functions.updates.GetStateRequest())
            self.record([],{'global':self.state(state)})
            self.queue.gap('initial_checkpoint_no_historical_replay',started_at=datetime.now(timezone.utc).isoformat())
        self.task=asyncio.create_task(self.loop())

    @staticmethod
    def state(s):
        return dict(pts=s.pts,qts=s.qts,date=s.date.isoformat(),seq=s.seq)

    async def global_difference(self):
        for _ in range(20):
            state=self.queue.get_state('global')
            r=await self.client(functions.updates.GetDifferenceRequest(pts=state['pts'],qts=state['qts'],
                date=datetime.fromisoformat(state['date'])))
            name=type(r).__name__
            if name=='DifferenceTooLong':
                self.queue.gap('telegram_difference_too_long',previous_pts=state['pts'],new_pts=r.pts)
                fresh=await self.client(functions.updates.GetStateRequest())
                self.record([],{'global':self.state(fresh)})
                return
            if name=='DifferenceEmpty':
                state.update(date=r.date.isoformat(),seq=r.seq)
                self.record([],{'global':state});return
            rows=[self.make_event(m,'message.created') for m in r.new_messages]
            for update in r.other_updates:
                rows.extend(self.updates(update))
                if type(update).__name__=='UpdateChannelTooLong':self.pending_channels.add(update.channel_id)
            new_state=getattr(r,'state',None) or r.intermediate_state
            self.record(rows,{'global':self.state(new_state)})
            if name=='Difference':return

    async def channel_difference(self,cid):
        key='channel:'+str(cid)
        pts=self.queue.get_state(key)
        if pts is None:
            self.queue.gap('channel_checkpoint_missing',channel_id=str(cid));return
        entity=await self.client.get_input_entity(types.PeerChannel(cid))
        for _ in range(20):
            r=await self.client(functions.updates.GetChannelDifferenceRequest(channel=entity,
                filter=types.ChannelMessagesFilterEmpty(),pts=pts,limit=100))
            name=type(r).__name__
            if name=='ChannelDifferenceTooLong':
                self.queue.gap('telegram_channel_difference_too_long',channel_id=str(cid),previous_pts=pts)
                rows=[self.make_event(m,'message.created') for m in r.messages]
                pts=r.dialog.pts
            else:
                rows=[self.make_event(m,'message.created') for m in getattr(r,'new_messages',[])]
                for update in getattr(r,'other_updates',[]):rows.extend(self.updates(update))
                pts=r.pts
            self.record(rows,{key:pts})
            if getattr(r,'final',True):return
        self.pending_channels.add(cid)

    async def loop(self):
        while self.active:
            try:
                if time.time()>=self.retry_at:
                    async with self.lock:
                        await self.global_difference()
                        pending=list(self.pending_channels)
                        for cid in pending[:20]:
                            self.pending_channels.discard(cid)
                            try:await self.channel_difference(cid)
                            except (errors.ChannelPrivateError,errors.ChannelInvalidError,ValueError) as exc:
                                self.queue.gap('channel_unavailable',channel_id=str(cid),error_type=type(exc).__name__)
                            except Exception:
                                self.pending_channels.add(cid);raise
                    self.last_success=time.time();self.error=None
                    self.queue.set_state('collector_last_success',self.last_success)
                self.queue.prune()
            except asyncio.CancelledError:raise
            except errors.FloodWaitError as exc:
                self.error='FloodWaitError';self.retry_at=time.time()+max(exc.seconds,30)
            except Exception as exc:
                self.error=type(exc).__name__;self.retry_at=time.time()+30
            await asyncio.sleep(15)

    async def stop(self):
        self.active=False
        if self.task:
            self.task.cancel()
            try:await self.task
            except asyncio.CancelledError:pass

    def status(self):
        return dict(active=self.active,connected=self.client.is_connected(),last_success=self.last_success,
            persisted_last_success=self.queue.get_state('collector_last_success'),last_error=self.error,
            retry_at=self.retry_at,pending_channel_recovery=len(self.pending_channels),
            coverage='Accessible cloud chats. Passive delivery and deletion coverage depend on Telegram; unrecoverable gaps are explicit.',
            queue=self.queue.stats(),notifications=False)
