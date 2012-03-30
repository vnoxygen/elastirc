from twisted.application import service
from twisted.application.internet import TimerService
from twisted.internet import task, protocol, defer, reactor, error
from twisted.python import log
from twisted.web.client import Agent, ResponseDone
from twisted.web.http_headers import Headers
from twisted.words.protocols import irc

from lxml import etree, html
from twittytwister.twitter import Twitter

import collections
import random
import shlex
import re

twitter_regexp = re.compile(r'twitter\.com/#!/[^/]+/status/(\d+)')

twatter = Twitter()

class LxmlStreamReceiver(protocol.Protocol):
    def __init__(self):
        self.deferred = defer.Deferred()
        self._parser = etree.XMLParser()

    def dataReceived(self, data):
        self._parser.feed(data)

    def connectionLost(self, reason):
        if reason.check(ResponseDone):
            self.deferred.callback(self._parser.close())
        else:
            self.deferred.errback(reason)

class HtmlStreamReceiver(protocol.Protocol):
    def __init__(self):
        self.deferred = defer.Deferred()
        self._buffer = []

    def dataReceived(self, data):
        self._buffer.append(data)

    def connectionLost(self, reason):
        if reason.check(ResponseDone):
            self.deferred.callback(html.fromstring(''.join(self._buffer)))
        else:
            self.deferred.errback(reason)

def paragraphCount(l):
    return sum(1 for x in l if x.strip())

@defer.inlineCallbacks
def mspaCounts(agent, urls):
    ret = collections.Counter()
    for url in urls:
        resp = yield agent.request('GET', url)
        receiver = HtmlStreamReceiver()
        resp.deliverBody(receiver)
        doc = yield receiver.deferred
        ret['pesterlines'] += paragraphCount(doc.xpath('//div[@class="spoiler"]//p//text()'))
        ret['paragraphs'] += paragraphCount(doc.xpath('//td[@bgcolor="#EEEEEE"]//center/p/text()'))
        ret['images'] += sum(1 for img in doc.xpath('//td[@bgcolor="#EEEEEE"]//img/@src') if 'storyfiles' in img)
        ret['flashes'] += sum(1 for src in doc.xpath('//td[@bgcolor="#EEEEEE"]//script/@src') if 'storyfiles' in src)
        ret['pages'] += 1

    defer.returnValue(ret)

class MSPAChecker(service.MultiService):
    def __init__(self, target):
        service.MultiService.__init__(self)
        self.target = target
        self.agent = Agent(reactor)
        self.timer = TimerService(120, self._doPoll)
        self.timer.setServiceParent(self)
        self._lastModified = self._lastLink = None

    def _doPoll(self):
        d = self._actuallyDoPoll()
        d.addErrback(log.err)
        return d

    @defer.inlineCallbacks
    def _actuallyDoPoll(self):
        headers = Headers()
        if self._lastModified is not None:
            headers.addRawHeader('If-Modified-Since', self._lastModified)
        try:
            resp = yield self.agent.request(
                'GET', 'http://www.mspaintadventures.com/rss/rss.xml',
                headers)
        except error.TimeoutError:
            log.msg('timeout requesting MSPA')
            return
        if resp.code == 304:
            return
        elif resp.code != 200:
            log.msg('strange HTTP code from MSPA: %r' % (resp.code,))
            return
        if resp.headers.hasHeader('Last-Modified'):
            self._lastModified, = resp.headers.getRawHeaders('Last-Modified')
        streamer = LxmlStreamReceiver()
        resp.deliverBody(streamer)
        doc = yield streamer.deferred
        prev = None
        newUrls = []
        for item in doc.xpath('/rss/channel/item'):
            link, = item.xpath('link/text()')
            title, = item.xpath('title/text()')
            if self._lastLink is None:
                self._lastLink = link
                return
            elif self._lastLink == link:
                if prev is None:
                    return
                break
            else:
                newUrls.append(link)
                prev = link, title
        self._lastLink, = doc.xpath('/rss/channel/item[1]/link/text()')
        newLink, newTitle = prev
        _, _, newTitle = newTitle.partition(' : ')
        log.msg('new MSPA: %r' % (newTitle,))
        targetClient = yield self.target.clientDeferred()
        targetClient.newMSPA(newLink, newTitle)
        counts = yield mspaCounts(self.agent, newUrls)
        targetClient.newMSPACounts(counts)

def _extractTwatText(twat):
    rt = twat.retweeted_status
    if rt:
        return u'RT @%s: %s' % (rt.user.screen_name, rt.text)
    else:
        return twat.text

class TheresaProtocol(irc.IRCClient):
    outstandingPings = 0
    _pinger = None
    _buttReady = True

    def _serverPing(self):
        if self.outstandingPings > 5:
            self.loseConnection()
        self.sendLine('PING bollocks')
        self.outstandingPings += 1

    def irc_PONG(self, prefix, params):
        self.outstandingPings -= 1

    def signedOn(self):
        self.factory.established(self)
        self.msg('nickserv', 'identify %s' % self.nickserv_pw)
        self.join(self.channel)
        self._pinger = task.LoopingCall(self._serverPing)
        self._pinger.start(60)

    def connectionLost(self, reason):
        irc.IRCClient.connectionLost(self, reason)
        self.factory.unestablished()
        if self._pinger is not None:
            self._pinger.stop()

    def ctcpQuery(self, user, channel, messages):
        messages = [(a.upper(), b) for a, b in messages]
        irc.IRCClient.ctcpQuery(self, user, channel, messages)

    def newMSPA(self, link, title):
        self.msg(self.channel, '%s (%s)' % (title, link))

    def newMSPACounts(self, counts):
        self.msg(self.channel, 'new: %s' % '; '.join('%s %s' % (v, k) for k, v in counts.iteritems() if v))

    def noticed(self, user, channel, message):
        pass

    def privmsg(self, user, channel, message):
        if not channel.startswith('#'):
            return

        for m in twitter_regexp.finditer(message):
            self.showTwat(channel, m.group(1))

        if not message.startswith(','):
            self.maybeRespondTo(channel, message)
            return

        splut = shlex.split(message[1:])
        command, params = splut[0], splut[1:]
        meth = getattr(self, 'command_%s' % (command.lower(),), None)
        if meth is not None:
            d = defer.maybeDeferred(meth, channel, *params)
            @d.addErrback
            def _eb(f):
                self.msg(channel, 'error in %s: %s' % (command, f.getErrorMessage()))
                return f
            d.addErrback(log.err)

    def maybeRespondTo(self, channel, message):
        if len(message) > 30 and random.randrange(6) == 0 and self._buttReady:
            words = re.split(r'(\s+|-)', message)
            buttified = False
            for e, word in enumerate(words):
                if word.isalnum() and len(word) <= 8 and random.randrange(7) == 0:
                    words[e] = 'butt'
                    buttified = True

            if buttified:
                self.msg(channel, ''.join(words))
                self._buttReady = False
                reactor.callLater(random.randrange(60, 300), self._becomeButtReady)

    def _becomeButtReady(self):
        self._buttReady = True

    def _twatDelegate(self, channel):
        return lambda twat: self.msg(
            channel,
            ('<%s> %s' % (twat.user.screen_name, _extractTwatText(twat))).encode('utf-8'))

    def showTwat(self, channel, id):
        return twatter.show(id, self._twatDelegate(channel))

    def command_twat(self, channel, user):
        return twatter.user_timeline(self._twatDelegate(channel), user,
                                     params=dict(count='1', include_rts='true'))

class TheresaFactory(protocol.ReconnectingClientFactory):
    protocol = TheresaProtocol

    def __init__(self):
        self._clientDeferred = defer.Deferred()
        self._client = None

    def established(self, protocol):
        self._client = protocol
        self._clientDeferred.callback(protocol)
        self.resetDelay()

    def unestablished(self):
        self._client = None
        self._clientDeferred = defer.Deferred()

    def clientDeferred(self):
        if self._client is not None:
            return defer.succeed(self._client)
        d = defer.Deferred()
        self._clientDeferred.chainDeferred(d)
        return d
