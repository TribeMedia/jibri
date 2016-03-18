#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
    Based on examples from SleekXMPP: https://github.com/fritzy/SleekXMPP
"""

import sleekxmpp
import logging
from sleekxmpp import Iq
from queue import Queue, Empty
from sleekxmpp.xmlstream import register_stanza_plugin, ElementBase, scheduler
from sleekxmpp.xmlstream.handler import Callback
from sleekxmpp.xmlstream.matcher import StanzaPath

class JibriElement(ElementBase):
    name = 'jibri'
    namespace = 'http://jitsi.org/protocol/jibri'
    plugin_attrib = 'jibri'
class JibriStatusElement(ElementBase):
    name = 'jibri-status'
    namespace = 'http://jitsi.org/protocol/jibri'
    plugin_attrib = 'jibri-status'

class JibriXMPPClient(sleekxmpp.ClientXMPP):

    def __init__(self, jid, password, room, nick, roompass, loop, jibri_start_callback, jibri_stop_callback, recording_lock, signal_queue):
        sleekxmpp.ClientXMPP.__init__(self, jid, password)

        self.room = room
        self.nick = nick
        self.roompass = roompass
        self.jid = jid
        self.jibri_start_callback = jibri_start_callback
        self.jibri_stop_callback = jibri_stop_callback
        self.recording_lock = recording_lock
        self.queue = signal_queue
        self.loop = loop
        self.controllerJid = ''

        self.add_event_handler("session_start", self.start)
        self.add_event_handler("muc::%s::got_online" % self.room,
                               self.muc_online)

        self.register_handler(
            Callback('Jibri IQ callback',
                     StanzaPath('iq@type=set/jibri'),
                     self.on_jibri_iq))


        register_stanza_plugin(Iq, JibriElement)
        register_stanza_plugin(Iq, JibriStatusElement)

    def on_jibri_iq(self, iq):
#        global running
#        global jibriiq
#        jibriiq = iq

        logging.info("on_jibri_iq: %s" % iq)

        reply = self.Iq()
        reply['to'] = iq['from']
        reply['id'] = iq['id']

        start = False
        stop = False
        action = iq['jibri']._getAttr('action')
        if action == 'start':
            #we hope for threadsafe, so don't block and just tell jicofo no....
            if not self.recording_lock.acquire(False):
                reply = self.make_iq_error(iq['id'], condition='service-unavailable', text='Instance already in use.', ito=iq['from'], iq=reply)
                reply._setAttr('code', 503)
#                reply['jibri']._setAttr('state', 'pending')
            else:
                if not iq['jibri']._getAttr('streamid'):
                    reply['type'] = 'error'
                    reply['error']['text'] = 'No stream-id.'
                elif not iq['jibri']._getAttr('url'):
                    reply['type'] = 'error'
                    reply['error']['text'] = 'No URL.'
                else:
                    running = True
                    reply['type'] = 'result'
                    reply['jibri']._setAttr('state', 'pending')
                    # We've found "the one". It's the first one. We're not choosy.
                    self.controllerJid = iq['from']
                    start = True
        elif action == 'stop':
            stop = True
            reply['type'] = 'result'
            reply['jibri']._setAttr('state', 'stopping')
        else:
            reply['type'] = 'error'
            reply['error']['text'] = 'Action not implemented.'
            logging.error("Action %s not implemented" % action)

        reply.send()

        if start:
            # Mark us as busy in the MUC presence:
            self.presence_busy()
            global client_in_use
            client_in_use = self
            # msg received, call the msg callback in the main thread with the event loop
            # this nets out a call to start_recording(client, url, follow_entity, stream_id)
            self.loop.call_soon_threadsafe(self.jibri_start_callback, self, iq['jibri']._getAttr('url'),iq['jibri']._getAttr('follow-entity'),iq['jibri']._getAttr('streamid'))

            #callback to parent thread to start jibri
            # TODO: notify of updates
        elif stop:
            logging.info("Stopping.")
            # msg received, call the msg callback in the main thread with the event loop
            # this nets out a call to stop_recording(client)
            self.loop.call_soon_threadsafe(self.jibri_stop_callback, 'xmpp_stop')

    def handle_queue_msg(self, msg):
        if msg== None:
            #got the message to quit, so stand down
            self.abort()
        if msg == 'idle':
            self.presence_idle()
        elif msg == 'busy': 
            self.presence_busy()
        elif msg == 'off':
            self.update_jibri_status('off')
        elif msg == 'on':
            self.update_jibri_status('on')
        elif msg == 'stopped':
            try:
                recording_lock.release()
            except:
                pass
            self.update_jibri_status('off')
        elif msg == 'started':
            self.update_jibri_status('on')

    def from_main_thread_nonblocking(self):
#        logging.info("Checking queue")
        try:
            msg = self.queue.get(False) #doesn't block
            logging.info("got msg from main: %s" % msg)
            # schedule the reply
            scheduler.Task("HANDLE REPLY", 0, self.handle_queue_msg, (msg,)).run()
        except Empty:
            pass

    def start(self, event):
        self.get_roster()
        self.send_presence()
        self.plugin['xep_0045'].joinMUC(self.room,
                                        self.nick,
                                        password=self.roompass)
                                        #wait=True)
        self.scheduler.add("asyncio_queue", 2, self.from_main_thread_nonblocking,
            repeat=True, qpointer=self.event_queue)


        #update our presence with
        #<jibri xmlns="http://jitsi.org/protocol/jibri status="idle" />
        self.presence_idle()

    def presence_busy(self):
        presence = self.make_presence(pto=self.room)
        tmp = self.Iq()
        tmp['jibri-status']._setAttr('status', 'busy')
        presence.append(tmp['jibri-status'])
        logging.info('sending presence: %s' % presence)
        presence.send()

    def presence_idle(self):
        presence = self.make_presence(pto=self.room)
        iq = self.Iq()
        iq['jibri-status']._setAttr('status', 'idle')
        presence.append(iq['jibri-status'])
        logging.info('sending presence: %s' % presence)
        presence.send()


    def update_jibri_status(self, status):
        iq = self.Iq()
        iq['to'] = self.controllerJid
        iq['type'] = 'set'
        iq['jibri']._setAttr('status', status)
        logging.info('sending status update: %s' % iq)
        try:
            iq.send()
        except Exception as e:
            logging.error("Failed to send status update: %s", str(e))

    def muc_online(self, presence):
        pass