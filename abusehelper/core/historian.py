import re
import time
import sqlite3
from datetime import datetime

import idiokit
from idiokit import timer
from idiokit.xmlcore import Element
from idiokit.xmpp.jid import JID
from abusehelper.core import taskfarm, events, bot, services

class HistoryDB(object):
    def __init__(self, path=None, keeptime=None):
        if path is None:
            path = ":memory:"
        self.conn = sqlite3.connect(path)

        cursor = self.conn.cursor()

        cursor.execute("CREATE TABLE IF NOT EXISTS events "+
                       "(id INTEGER PRIMARY KEY, timestamp INTEGER, room INTEGER)")
        cursor.execute("CREATE INDEX IF NOT EXISTS events_id_index ON events(id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS events_room_ts_index ON events(room, timestamp)")
        cursor.execute("CREATE INDEX IF NOT EXISTS events_room_index ON events(room)")
        cursor.execute("CREATE INDEX IF NOT EXISTS events_ts_index ON events(timestamp)")

        cursor.execute("CREATE TABLE IF NOT EXISTS attrs "+
                       "(eventid INTEGER, key TEXT, value TEXT)")
        cursor.execute("CREATE INDEX IF NOT EXISTS attrs_eventid_index ON attrs(eventid)")

        self.conn.commit()
        self.keeptime = keeptime
        self.cursor = self.conn.cursor()

        self.main = self._main()

    @idiokit.stream
    def collect(self, room_name):
        collect = self._collect(room_name)
        idiokit.pipe(self.main.fork(), collect)

        try:
            yield collect
        except:
            self.main.throw()
            raise

    @idiokit.stream
    def _collect(self, room_name):
        while True:
            event = yield idiokit.next()
            if event.contains("bot:action"):
                continue

            self.cursor.execute("INSERT INTO events(timestamp, room) VALUES (?, ?)",
                                (int(time.time()), room_name))
            eventid = self.cursor.lastrowid

            for key in event.keys():
                values = event.values(key)
                self.cursor.executemany("INSERT INTO attrs(eventid, key, value) VALUES (?, ?, ?)",
                                        [(eventid, key, value) for value in values])

    @idiokit.stream
    def _main(self, interval=1.0):
        try:
            while True:
                yield timer.sleep(interval)

                if self.keeptime is not None:
                    cutoff = int(time.time() - self.keeptime)

                    max_id = self.cursor.execute("SELECT MAX(events.id) FROM events "+
                                                 "WHERE events.timestamp <= ?", (cutoff,))

                    max_id = list(max_id)[0][0]
                    if max_id is not None:
                        self.cursor.execute("DELETE FROM events WHERE events.id <= ?",
                                            (max_id,))
                        self.cursor.execute("DELETE FROM attrs WHERE attrs.eventid <= ?",
                                            (max_id,))
                self.conn.commit()
                self.cursor = self.conn.cursor()
        finally:
            self.conn.commit()
            self.conn.close()

    def close(self):
        self.main.throw(StopIteration, StopIteration(), None)

    def find(self, room_name=None, start=None, end=None):
        query = ("SELECT events.id, events.room, events.timestamp, attrs.key, attrs.value "+
                 "FROM attrs "+
                 "INNER JOIN events ON events.id=attrs.eventid ")
        args = list()
        where = list()

        if room_name is not None:
            where.append("events.room = ?")
            args.append(room_name)

        if None not in (start, end):
            where.append("events.timestamp BETWEEN ? AND ?")
            args.append(start)
            args.append(end)
        elif start is not None:
            where.append("events.timestamp >= ?")
            args.append(start)
        elif end is not None:
            where.append("events.timestamp < ?")
            args.append(end)

        if where:
            query += "WHERE " + " AND ".join(where) + " "

        query += "ORDER BY events.id"

        event = events.Event()
        previous_id = None
        previous_ts = None
        previous_room = None
        for id, room, ts, key, value in self.conn.execute(query, args):
            if previous_id != id:
                if previous_id is not None:
                    yield previous_ts, previous_room, event
                event = events.Event()

            previous_id = id
            previous_ts = ts
            previous_room = room

            event.add(key, value)

        if previous_id is not None:
            yield previous_ts, previous_room, event

def format_time(timestamp, format="%Y-%m-%d %H:%M:%S"):
    return time.strftime(format, time.localtime(timestamp))

def iso_to_unix(iso_time, format=None):
    if format:
        return time.mktime(datetime.strptime(iso_time, format).timetuple())

    try:
        f = "%Y-%m-%d %H:%M:%S"
        return time.mktime(datetime.strptime(iso_time, f).timetuple())
    except ValueError:
        try:
            f = "%Y-%m-%d %H:%M"
            return time.mktime(datetime.strptime(iso_time, f).timetuple())
        except ValueError:
            f = "%Y-%m-%d"
            return time.mktime(datetime.strptime(iso_time, f).timetuple())

def delay_element(timestamp):
    if time.daylight:
        timestamp = timestamp + time.altzone
    else:
        timestamp = timestamp + time.timezone
    delay = Element("delay")
    delay.set_attr("xmlns", 'urn:xmpp:delay')
    delay.set_attr("stamp", format_time(timestamp, '%Y-%m-%dT%H:%M:%SZ'))
    return delay

def parse_command(message, name):
    parts = message.text.split()
    if not len(parts) >= 2:
        return None, None, None
    command = parts[0][1:]
    if command != name:
        return None, None, None

    params = " ".join(parts[1:])

    start = list()
    end = list()
    keyed = dict()
    values = set()
    regexp = r'(\S+="[\S\s]+?")|(\S+=\S+)|("\S+\s\S+")|(\S+)'
    for match in re.findall(regexp, params):
        for group in match:
            if not group:
                continue

            pair = group.split('=')

            if len(pair) == 1:
                value = pair[0]
                if value.startswith('"') and value.endswith('"'):
                    value = value[1:-1]
                if value:
                    values.add(value)
            elif len(pair) >= 2:
                value = "=".join(pair[1:])
                if value.startswith('"') and value.endswith('"'):
                    value = value[1:-1]

                if value:
                    if pair[0] == "start":
                        try:
                            start.append(iso_to_unix(value))
                        except:
                            keyed.setdefault(pair[0], set())
                            keyed[pair[0]].add(value)
                    elif pair[0] == "end":
                        try:
                            end.append(iso_to_unix(value))
                        except:
                            keyed.setdefault(pair[0], set())
                            keyed[pair[0]].add(value)
                    else:
                        keyed.setdefault(pair[0], set())
                        keyed[pair[0]].add(value)

    if start:
        start = sorted(start).pop(0)
    else:
        start = None

    if end:
        end = sorted(end).pop()
    else:
        end = None

    def _match(event):
        for key, keyed_values in keyed.iteritems():
            if keyed_values.intersection(event.values(key)):
                return True
        if values.intersection(event.values()):
            return True
        return False

    return _match, start, end

class HistorianService(bot.ServiceBot):
    def __init__(self, bot_state_file=None, **keys):
        bot.ServiceBot.__init__(self, bot_state_file=None, **keys)
        self.history = HistoryDB(bot_state_file)
        self.rooms = taskfarm.TaskFarm(self.handle_room)

    @idiokit.stream
    def main(self, state):
        try:
            yield self.xmpp.fork() | self.query_handler()
        except services.Stop:
            idiokit.stop()

    @idiokit.stream
    def handle_room(self, name):
        self.log.info("Joining room %r", name)
        room = yield self.xmpp.muc.join(name, self.bot_name)
        self.log.info("Joined room %r", name)

        try:
            yield idiokit.pipe(room,
                               self.skip_own(room),
                               events.stanzas_to_events(),
                               self.history.collect(unicode(room.jid.bare())))
        finally:
            self.log.info("Left room %r", name)

    @idiokit.stream
    def session(self, state, src_room):
        try:
            yield self.rooms.inc(src_room)
        except services.Stop:
            idiokit.stop()

    @idiokit.stream
    def skip_own(self, room):
        while True:
            element = yield idiokit.next()

            for owned in element.with_attrs("from"):
                sender = JID(owned.get_attr("from"))
                if room.jid != sender:
                    yield idiokit.send(owned)

    @idiokit.stream
    def query_handler(self):
        while True:
            element = yield idiokit.next()

            for message in element.named("message").with_attrs("from"):
                sender = JID(message.get_attr("from"))
                room_jid = sender.bare()
                chat_type = message.get_attr("type")

                if chat_type == "groupchat":
                    attrs = dict(type=chat_type)
                    to = room_jid
                else:
                    attrs = dict()
                    to = sender

                if room_jid not in self.xmpp.muc.rooms:
                    return

                if room_jid == self.service_room:
                    room_jid = None
                else:
                    room_jid = unicode(room_jid)

                yield self.command_parser(element, to, room_jid, **attrs)

    @idiokit.stream
    def command_parser(self, message, requester, room_jid, **attrs):
        for body in message.children("body"):
            matcher, start, end = parse_command(body, "historian")
            if matcher is None:
                continue

            self.log.info("Got command %r, responding to %r", body.text,
                                                              requester)
            counter = 0
            for etime, eroom, event in self.history.find(room_jid, start, end):
                if not matcher(event):
                    continue

                body = Element("body")
                body.text = "%s %s\n" % (format_time(etime), eroom)
                for key in event.keys():
                    vals = ", ".join(event.values(key))
                    body.text += "%s: %s\n" % (key, vals)

                elements = [body]
                yield self.xmpp.core.message(requester, *elements, **attrs)
                counter += 1

            self.log.info("Returned %i events.", counter)

if __name__ == "__main__":
    HistorianService.from_command_line().execute()
