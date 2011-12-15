import urllib2
import xml.etree.cElementTree as etree
from time import strftime, strptime

import idiokit
from abusehelper.core import bot, events, utils, config, cymru
from abusehelper.contrib.rssbot.rssbot import RSSBot

class ProjectHoneyPotBot(RSSBot):
    feeds = bot.ListParam(default=[
            "http://www.projecthoneypot.org/list_of_ips.php?rss=1"])

    def augment(self):
        return cymru.CymruWhois()

    def create_event(self, **kw):
        self.log.info("Got %r", kw)
        if not kw.get('description', ''):
            return None
        description = kw['description'].split(' | ')
        if len(description) < 2:
            return None
        if not kw.get('title', ''):
            return None
        title = kw.get('title').split(' | ')
        if not len(title) == 2:
            return None
        ip, badness = title

        event = events.Event()
        if kw.get('source', ''):
            event.add('source', kw.get('source'))
        event.add('ip', ip)
        event.add('url', 'http://www.projecthoneypot.org/ip_%s' % (ip))

        badtypes = {'H': 'spam harvester', 
                    'S': 'mail server', 
                    'D': 'dictionary attacker', 
                    'W': 'bad web host',
                    'C': 'comment spammer'}

        for item in badtypes:
            if item in badness:
                event.add('type', badtypes[item])

        descritems = [x.strip().split(': ') for x in 
                      description[1:]]

        descrtypes = {'Total': 'count',
                      'First': 'firstseen',
                      'Last': 'lastseen'}
        for key, val in descritems:
            if key in descrtypes:
                if key == 'Total':
                    val = val.replace(',', '')
                event.add(descrtypes[key], val)

        if kw.get('pubDate', ''):
            pubdate = kw.get('pubDate')
            try:
                ts = strptime(pubdate, '%B %d %Y %I:%M:%S %p')
                pubtime = strftime("%Y-%m-%d %H:%M:%S", ts)
                
                event.add('pubtime', pubtime)
            except ValueError:
                pass

        return event

if __name__ == "__main__":
    ProjectHoneyPotBot.from_command_line().run()
