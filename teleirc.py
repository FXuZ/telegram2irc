#! /usr/bin/env python3

import sys
import re
import threading
import pickle
import ssl
import time

import irc.client

from telegram import Telegram
from config import config

help_txt = {
    'all'  : 'current avaliable commands are: .nick, .help, .join, .list',
    'help' : '.help [command] => show help message (for `command`).',
    'nick' : '.nick <new_nick> => change your nick to `new_nick`, no space allowed.',
    'join' : '.join <channel> [channel [channel [...]]] => join `channel`(s). Use `.list` to list avaliable channels.',
    'list' : '.list => list all avaliable chats.',
}

msg_format = '[{nick}] {msg}'

tele_conn = None
irc_conn = None
bindings = tuple()
usernicks = {}

irc_channels = []
tele_me = None

irc_blacklist = []


def splitmsg(m, size):
    b = m.encode('utf-8')
    if len(b) <= size:
      yield m
    else:
      prefix = b[:size].decode('utf-8', errors='ignore')
      yield prefix
      yield from splitmsg(m[len(prefix):], size)

def on_pong(connection, event):
    connection.last_pong = time.time()
    print('[irc]  PONG from: ', event.source)

def on_connect(connection, event):
    for c in irc_channels:
        if irc.client.is_channel(c):
            connection.join(c)

def on_join(connection, event):
    print('[irc] ', event.source + ' ' + event.target)

def on_privmsg(connection, event):
    print('[irc] ', event.source + ' ' + event.target + ' ' + event.arguments[0])

    tele_target = get_tele_binding(event.target)
    irc_nick = event.source[:event.source.index('!')]
    msg = event.arguments[0]

    if tele_target is not None and irc_nick not in irc_blacklist:
        tele_conn.send_msg(
                tele_target,
                msg_format.format(
                    nick = irc_nick,
                    msg = msg
                )
        )

def on_nickinuse(connection, event):
    connection.nick(connection.get_nickname() + '_')

def main_loop():
    def irc_thread():
        reactor = irc_init()
        reactor.process_forever(60)

    def tele_thread():
        tele_init()
        while True:
            msg = tele_conn.recv_one_msg()
            if len(msg) == 3:
                # FixMe: dirty-hack, user info  -bigeagle
                # FIXME: yeah, it's a little bit dirty, and it
                # has a small bug, people's nick will still be
                # number if it is the first time (s)he send a
                # massage. But it is all my fault, not bigeagle's,
                # because the main code here is poorly designed.
                # QAQ -WaterA
                userid, username, realname = msg
                change_usernick(userid, username or realname)
                continue
            if msg == -1:
                break

            elif msg is not None and msg[2] != tele_me:
                _time, chatid, userid, content = msg
                print('[tel] ', *msg)
                if chatid is not None:
                    # msg is from chat group
                    irc_target = get_irc_binding('chat#'+chatid)
                elif content.startswith('.'):
                    # msg is from user and is a command
                    handle_command(msg)
                    irc_target = None
                elif re.match(r'.?help\s*$', content):
                    # msg is from user and user needs help
                    send_help(userid)
                    irc_target = None
                else:
                    # msg is from user and is not a command
                    irc_target = get_irc_binding('user#'+userid)

                if irc_target is not None:
                    nick = get_usernick_from_id(userid)
                    if nick is None:
                        tele_conn.get_user_info(userid)
                        nick = userid
                    lines = content.split('\n')
                    for line in lines:
                        for sline in splitmsg(line, 510):  # limit message length to 512
                            irc_conn.privmsg(irc_target, msg_format.format(nick=nick, msg=sline))

    tasks = []
    for i in (irc_thread, tele_thread):
        t = threading.Thread(target=i, args=())
        t.setDaemon(True)
        t.start()
        tasks.append(t)

    for t in tasks:
        t.join()


def get_irc_binding(tele_chat):
    for b in bindings:
        if b[1] == tele_chat:
            return b[0]
    return None

def get_tele_binding(irc_chan):
    for b in bindings:
        if b[0] == irc_chan:
            return b[1]
    return None

def get_usernick_from_id(userid):
    return usernicks.get(userid, None)

def change_usernick(userid, newnick):
    usernicks[userid] = newnick
    save_usernicks()

def send_help(userid, help='all'):
    try:
        m = help_txt[help]
    except KeyError:
        m = help_txt['all']

    tele_conn.send_user_msg(userid, m)

def invite_to_join(userid, chatlist):
    for c in chatlist:
        chat = get_tele_binding(c)

        if chat is not None:
            cmd = 'chat_add_user {chat} {user} 0'.format(
                chat=chat,
                user='user#' + userid
            )
            tele_conn.send_cmd(cmd)
        else:
            tele_conn.send_user_msg(userid, '{0} is not avaliable. Use `.list` to see avaliable channels'.format(c))

def handle_command(msg):
    if not msg[3].startswith('.'):
        return

    userid = msg[2]
    try:
        tmp = msg[3].split()
        cmd = tmp[0][1:].lower()
        args = tmp[1:]
    except IndexError:
        send_help(userid)

    if cmd == 'nick':
        try:
            change_usernick(userid, args[0])
            tele_conn.send_user_msg(userid, 'Your nick has changed to {0}'.format(args[0]))
        except IndexError:
            send_help(userid, 'nick')
    elif cmd == 'help':
        try:
            send_help(userid, args[0])
        except IndexError:
            send_help(userid, 'help')
            send_help(userid)
    elif cmd == 'join':
        if len(args) == 0:
            send_help(userid, 'join')
        invite_to_join(userid, args)
    elif cmd == 'list':
        chan = ', '.join([i[0] for i in bindings])
        tele_conn.send_user_msg(userid, chan)
    else:
        send_help(userid)

def irc_init():
    global irc_channels
    global irc_conn

    irc_channels = [i[0] for i in config['bindings']]
    server = config['irc']['server']
    port = config['irc']['port']
    nickname = config['irc']['nick']
    usessl = config['irc']['ssl']

    # use a replacement character for unrecognized byte sequences
    # see <https://pypi.python.org/pypi/irc>
    irc.client.ServerConnection.buffer_class.errors = 'replace'

    reactor = irc.client.Reactor()

    irc_conn = reactor.server()
    try:
        if usessl:
            ssl_factory = irc.connection.Factory(wrapper=ssl.wrap_socket)
            irc_conn.connect(server, port, nickname,
                    connect_factory=ssl_factory)
        else:
            irc_conn.connect(server, port, nickname)
    except irc.client.ServerConnectionError:
        print(sys.exc_info()[1])

    irc_conn.add_global_handler("welcome", on_connect)
    irc_conn.add_global_handler("join", on_join)
    irc_conn.add_global_handler("privmsg", on_privmsg)
    irc_conn.add_global_handler("pubmsg", on_privmsg)
    irc_conn.add_global_handler("action", on_privmsg)
    irc_conn.add_global_handler("pong", on_pong)
    irc_conn.add_global_handler("nicknameinuse", on_nickinuse)

    irc_conn.last_pong = time.time()

    def keep_alive_ping(connection):
        try:
            if time.time() - connection.last_pong > 360:
                raise irc.client.ServerNotConnectedError('ping timeout!')
                connection.last_pong = time.time()
            connection.ping(connection.get_server_name())
        except irc.client.ServerNotConnectedError:
            print('[irc]  Reconnecting...')
            connection.reconnect()
            connection.last_pong = time.time()

    reactor.execute_every(60, keep_alive_ping, (irc_conn,))

    return reactor

def tele_init():
    global tele_conn
    global tele_me

    server = config['telegram']['server']
    port = config['telegram']['port']
    tele_me = config['telegram']['me'].replace('user#', '')
    tele_conn = Telegram(server, port)

def load_usernicks():
    global usernicks
    try:
        with open('usernicks', 'rb') as f:
            usernicks = pickle.load(f)
    except Exception:
        usernicks = {}

def save_usernicks():
    global usernicks
    try:
        with open('usernicks', 'wb') as f:
            pickle.dump(usernicks, f, pickle.HIGHEST_PROTOCOL)
    except Exception:
        pass

def main():
    global bindings
    global irc_blacklist

    bindings = config['bindings']
    irc_blacklist = config['irc']['blacklist']
    load_usernicks()

    try:
        main_loop()
    except (Exception, KeyboardInterrupt):
        try:
            irc_conn.quit('Bye')
            irc_conn = None
            tele_conn = None # to call __del__ method of Telegram to close connection
        except Exception:
            pass
    finally:
        print('Bye.')

if __name__ == '__main__':
    main()
