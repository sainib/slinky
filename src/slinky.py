#!/usr/bin/env python
# encoding: utf-8

## Copyright (C) 2010, Paco Nathan. This work is licensed under
## the BSD License. To view a copy of this license, visit:
##    http://creativecommons.org/licenses/BSD/
## or send a letter to:
##    Creative Commons, 171 Second Street, Suite 300
##    San Francisco, California, 94105, USA
##
## @author Paco Nathan <ceteri@gmail.com>

from BeautifulSoup import BeautifulSoup, BeautifulStoneSoup, Comment, SoupStrainer
from datetime import datetime, timedelta
import Queue
import base64
import hashlib
import httplib
import random
import re
import redis
import robotparser
import string
import sys
import threading
import time
import urllib2
          

######################################################################
## global variables, debugging, dependency injection.. oh my!

start_time = datetime.now()
task_counter = 0

red_cli = None
opener = None

domain_rules = {}
conf_param = {}

CONF_PARAM_KEY = "conf"
conf_param["debug_level"] = "0"


redirect_handler = urllib2.HTTPRedirectHandler()

class MyHTTPRedirectHandler (urllib2.HTTPRedirectHandler):
    def http_error_302 (self, req, fp, code, msg, headers):
        printLog("REDIRECT", [code, req])
        return urllib2.HTTPRedirectHandler.http_error_302(self, req, fp, code, msg, headers)

    http_error_301 = http_error_303 = http_error_307 = http_error_302


######################################################################
## class definitions

class WebPage ():
    """Web Page representation"""

    def __init__ (self, protocol, domain, uuid, orig_url):
        ## initialize this Page

        self.protocol = protocol
        self.domain = domain
        self.uuid = uuid
        self.orig_url = orig_url
        self.norm_uuid = self.uuid
        self.norm_uri = self.orig_url
        self.crawl_time = 0
        self.seed_hops = 0
        self.status = "403"
        self.reason = "okay"
        self.content_type = ""
        self.date = ""
        self.checksum = ""
        self.b64_html = ""
        self.raw_html = ""


    def calcCrawlTime (self, crawl_start):
        ## calculate the period required for fetching this Page

        td = (datetime.now() - crawl_start)
        self.crawl_time = (td.microseconds + (td.seconds + td.days * 24 * 3600) * 10**6) / 10**6


    def getPage (self, url_handle):
        ## get HTTP headers and HTML content from a URI handle

        # keep track of the HTTP status and normalized URI
        self.status = str(url_handle.getcode())
        self.norm_uri = url_handle.geturl()
        self.norm_uuid, self.norm_uri = getUUID(self.norm_uri)

        # GET thresholded byte count of HTML content
        self.raw_html = url_handle.read(int(conf_param["max_page_len"]))

        # determine the content type

        url_info = url_handle.info()
        self.content_type = "text/html"

        if "content-type" in url_info:
            self.content_type = url_info["content-type"].split(";", 1)[0].strip()

        # determine a date

        self.date = ""

        if "last-modified" in url_info:
            self.date = url_info["last-modified"]
        else:
            self.date = url_info["date"]

        # create an MD5 checksum and Base64 encoding of the content

        m = hashlib.md5()
        m.update(self.raw_html)

        self.checksum = m.hexdigest()
        self.b64_html = base64.b64encode(self.raw_html)


    def checkRobots (self, uri):
        ## check "robots.txt" permission for this domain

        if self.domain in domain_rules:
            rp = domain_rules[self.domain]
        else:
            rp = robotparser.RobotFileParser()
            rp.set_url("/".join([self.protocol, "", self.domain, "robots.txt"]))
            rp.read()
            domain_rules[self.domain] = rp

        is_allowed = rp.can_fetch(conf_param["user_agent"], uri)

        if debug(3):
            printLog("ROBOTS", [is_allowed, uri])

        return is_allowed


    def fetch (self):
        ## attempt to fetch the given URI, collecting the status code

        try:
            # apply "robots.txt" restrictions, fetch if allowed

            if self.checkRobots(self.orig_url):
                url_handle = urllib2.urlopen(self.orig_url, None, int(conf_param["http_timeout"]))
                self.getPage(url_handle)

            # status codes based on HTTP/1.1 spec in RFC 2616:
            # http://www.w3.org/Protocols/rfc2616/rfc2616-sec10.html

        except httplib.InvalidURL, err:
            sys.stderr.write("HTTP InvalidURL: %(err)s\n%(data)s\n" % {"err": str(err), "data": self.orig_url})
            self.status = "400"
            self.reason = "invalid URL"
        except httplib.BadStatusLine, err:
            sys.stderr.write("HTTP BadStatusLine: %(err)s\n%(data)s\n" % {"err": str(err), "data": self.orig_url})
            self.status = "400"
            self.reason = "bad status line"
        except httplib.IncompleteRead, err:
            sys.stderr.write("HTTP IncompleteRead: %(err)s\n%(data)s\n" % {"err": str(err), "data": self.orig_url})
            self.status = "400"
            self.reason = "imcomplete read"
        except urllib2.HTTPError, err:
            sys.stderr.write("HTTPError: %(err)s\n%(data)s\n" % {"err": str(err), "data": self.orig_url})
            self.status = str(err.code)
            self.reason = "http error"
        except urllib2.URLError, err:
            sys.stderr.write("URLError: %(err)s\n%(data)s\n" % {"err": str(err), "data": self.orig_url})
            self.status = "400"
            self.reason = err.reason
        except IOError, err:
            sys.stderr.write("IOError: %(err)s\n%(data)s\n" % {"err": str(err), "data": self.orig_url})
            self.status = "400"
            self.reason = "IO error"
        except ValueError, err:
            # unknown url type: http
            sys.stderr.write("ValueError: %(err)s\n%(data)s\n" % {"err": str(err), "data": self.orig_url})
            self.status = "400"
            self.reason = "value error"
        else:
            if debug(3):
                printLog("SUCCESS", [self.orig_url])

        # TODO: capture explanation text for status code

        if debug(0):
            printLog("CRAWL", [self.domain, self.norm_uri, self.status, self.content_type, self.date, str(len(self.raw_html)), self.checksum])


    def markVisited (self):
        ## mark as "visited", not needing a crawl

        red_cli.setnx(self.norm_uuid, self.norm_uri)
        red_cli.sadd(conf_param["visited_set_key"], self.norm_uuid)

        if self.status.startswith("2"):
            red_cli.lpush(conf_param["needs_text_key"], self.norm_uuid)


    def persist (self):
        ## persist metadata + data for this Page

        red_cli.hset(self.norm_uuid, "uri", self.norm_uri)
        red_cli.hset(self.norm_uuid, "domain", self.domain)
        red_cli.hset(self.norm_uuid, "status", self.status)
        red_cli.hset(self.norm_uuid, "reason", self.reason)
        red_cli.hset(self.norm_uuid, "date", self.date)
        red_cli.hset(self.norm_uuid, "content_type", self.content_type)
        red_cli.hset(self.norm_uuid, "page_len", str(len(self.raw_html)))
        red_cli.hset(self.norm_uuid, "crawl_time", self.crawl_time)
        red_cli.hset(self.norm_uuid, "checksum", self.checksum)
        red_cli.hset(self.norm_uuid, "html", self.b64_html)
        red_cli.hset(self.norm_uuid, "out_links", "\t".join(self.out_links))


class ThreadUri (threading.Thread):
    """Threaded URI Fetcher"""

    def __init__ (self, local_queue, white_list):
        ## initialize this Thread

        threading.Thread.__init__(self)
        self.local_queue = local_queue
        self.white_list = white_list


    def getOutLinks (self, protocol, domain, raw_html):
        ## scan for outbound web links in the fetched HTML content

        out_links = set([])

        try:
            results = BeautifulSoup(raw_html, parseOnlyThese=SoupStrainer("a"))

            for link in results:
                if link.has_key("href"):
                    l = link["href"]

                    if len(l) < 1 or l.startswith("javascript") or l.startswith("#"):
                        # ignore non-links
                        pass
                    elif l.startswith("/"):
                        # reconstruct absolute URI from relative URI
                        out_links.add("/".join([protocol, "", domain]) + l)
                    else:
                        # add the absolute URI
                        out_links.add(l)
        except UnicodeEncodeError, err:
            sys.stderr.write("UnicodeEncodeError: %(err)s\n" % {"err": str(err)})

        if debug(4):
            printLog("OUT LINKS", out_links)

        return out_links


    def dequeueTask (self):
        # pop random/next from URI Queue and attempt to fetch HTML content

        [protocol, domain, uuid, orig_url] = self.local_queue.get()

        page = WebPage(protocol, domain, uuid, orig_url)
        crawl_start = datetime.now()

        page.fetch()
        page.calcCrawlTime(crawl_start)

        page.out_links = self.getOutLinks(page.protocol, page.domain, page.raw_html)

        # update the Page Store with fetched/analyzed data

        red_cli.srem(conf_param["pend_queue_key"], page.uuid)
        page.markVisited()

        # push HTTP-redirected link onto URI Queue

        if page.status.startswith("3"):
            self.enqueueLink(page.norm_uuid, page.norm_uri)

        # resolve redirects among the outbound links

        red_cli.hset(page.orig_url, "norm_uri", page.norm_uri)
        page.persist()

        return page.out_links


    def enqueueLink (self, uuid, uri):
        ## enqueue outbound links which satisfy white-listed domain rules

        protocol, domain = getDomain(uri)

        if debug(3):
            printLog("CHECK", [protocol, domain, uri])

        if len(self.white_list) < 1 or domain in self.white_list:
            testSet(uuid, uri)

            if debug(3):
                printLog("ENQUEUE", [domain, uuid, uri])


    def run (self):
        global task_counter

        while True:
            for link_uri in self.dequeueTask():
                link_uuid, link_uri = getUUID(link_uri)
                self.enqueueLink(link_uuid, link_uri)

            # progress report

            if debug(0):
                task_counter += 1
                td = (datetime.now() - start_time)
                printLog("TIME", [task_counter, (td.microseconds + (td.seconds + td.days * 24 * 3600) * 10**6) / 10**6])

            # after "throttled" wait period, signal to queue that the task completed
            # TODO: adjust "throttle" based on aggregate server load (?? Yan, let's define)

            time.sleep(int(conf_param["request_sleep"]) * (1.0 + random.random()))
            self.local_queue.task_done()
          

######################################################################
## lifecycle methods

def init (host_port_db):
    ## set up the Redis client

    host, port, db = host_port_db.split(":")
    return redis.Redis(host=host, port=int(port), db=int(db))


def printLog (kind, args):
    sys.stderr.write(kind + ": " + " ".join(map(lambda x: str(x), args)) + "\n")


def debug (level):
    return int(conf_param["debug_level"]) > level


def config ():
    ## put config params in key/value store, as a distrib cache

    for line in sys.stdin:
        try:
            line = line.strip()

            if len(line) > 0 and not line.startswith("#"):
                [param, value] = line.split("\t")

                printLog("CONFIG", [param, value])
                red_cli.hset(CONF_PARAM_KEY, param, value);

        except ValueError, err:
            sys.stderr.write("ValueError: %(err)s\n%(data)s\n" % {"err": str(err), "data": line})
        except IndexError, err:
            sys.stderr.write("IndexError: %(err)s\n%(data)s\n" % {"err": str(err), "data": line})


def flush (all):
    ## flush either all of the key/value store, or just the URI Queue

    if all:
        red_cli.flushdb()
    else:
        red_cli.delete(conf_param["todo_queue_key"])
        red_cli.delete(conf_param["pend_queue_key"])


def testSet (uuid, hops, uri):
    ## test whether URI has been visited before, then add to URI Queue

    visited_before = True

    if red_cli.setnx(uuid, uri):
        visited_before = False
        red_cli.zadd(conf_param["todo_queue_key"], hops, uuid)

    return visited_before


def seed ():
    ## seed the Queue with root URIs as starting points

    for line in sys.stdin:
        try:
            line = line.strip()
            [uri] = line.split("\t")
            uuid, uri = getUUID(uri)

            if debug(3):
                printLog("UUID", [uuid, uri])

            testSet(uuid, uri)

        except ValueError, err:
            sys.stderr.write("ValueError: %(err)s\n%(data)s\n" % {"err": str(err), "data": line})
        except IndexError, err:
            sys.stderr.write("IndexError: %(err)s\n%(data)s\n" % {"err": str(err), "data": line})


def putWhitelist ():
    ## push white-listed domain rules into key/value store

    for line in sys.stdin:
        try:
            line = line.strip()
            [domain] = line.split("\t")

            if debug(0):
                printLog("WHITELISTED", [domain])

            red_cli.sadd(conf_param["white_list_key"], domain)

        except ValueError, err:
            sys.stderr.write("ValueError: %(err)s\n%(data)s\n" % {"err": str(err), "data": line})
        except IndexError, err:
            sys.stderr.write("IndexError: %(err)s\n%(data)s\n" % {"err": str(err), "data": line})


def putStopwords ():
    ## push stopwords list into key/value store

    for line in sys.stdin:
        try:
            line = line.strip()
            [word] = line.split("\t")

            if debug(0):
                printLog("STOPWORDS", [word])

            red_cli.sadd(conf_param["stop_word_key"], word)

        except ValueError, err:
            sys.stderr.write("ValueError: %(err)s\n%(data)s\n" % {"err": str(err), "data": line})
        except IndexError, err:
            sys.stderr.write("IndexError: %(err)s\n%(data)s\n" % {"err": str(err), "data": line})


######################################################################
## crawler methods

def spawnThreads (local_queue, white_list, num_threads):
    ## spawn a pool of threads, passing them the local queue instance

    for i in range(num_threads):
        t = ThreadUri(local_queue, white_list)
        t.setDaemon(True)
        t.start()


def getUUID (uri):
    ## clean URI so it can be used as a unique key, then make an MD5 has as a UUID

    uri = uri.replace(" ", "+")
    m = hashlib.md5()
    m.update(filter(lambda x: x in string.printable, uri))
    uuid = m.hexdigest()

    return uuid, uri


def getDomain (uri):
    ## extract the domain from the URI

    protocol = "http:"
    domain = "foo.com"

    try:
        l = uri.split("/")
        protocol = l[0]
        domain = l[2]
    except IndexError, err:
        # may ignore these, defaults get used
        pass

    return protocol, domain


def resolveLinks (out_links):
    ## perform lookup on outbound links to resolve orig_url -> norm_uri mapping

    resolved_links = set([])

    for link_uri in out_links:
        norm_uri = red_cli.hget(link_uri, "norm_uri")

        if norm_uri:
            resolved_links.add(norm_uri)
        else:
            resolved_links.add(link_uri)

    return resolved_links


def drawQueue (local_queue, draw_limit):
    ## draw from the URI Queue to populate local queue with URI fetch tasks

    for n in range(1, draw_limit):
        uuid = red_cli.srandmember(conf_param["todo_queue_key"])

        if not uuid:
            # URI Queue is empty; have we reached DONE condition?
            pend_len = red_cli.scard(conf_param["pend_queue_key"])

            if debug(0):
                printLog("DONE: URI Queue is now empty", [])
                printLog("PEND", [pend_len])

            return pend_len > 0

        elif red_cli.smove(conf_param["todo_queue_key"], conf_param["pend_queue_key"], uuid):
            # get URI, determine domain

            uri = red_cli.get(uuid)
            protocol, domain = getDomain(uri)

            if debug(3):
                printLog("DOMAIN", [domain, uuid, uri])

            # enqueue URI fetch task in local queue
            local_queue.put([protocol, domain, uuid, uri])

    # there may be more iterations; not DONE
    return True


def crawl ():
    ## draw URI fetch tasks from URI Queue, populate local queue, run tasks in parallel

    printLog("START", [start_time])

    local_queue = Queue.Queue()
    white_list = red_cli.smembers(conf_param["white_list_key"])

    spawnThreads(local_queue, white_list, int(conf_param["num_threads"]))

    opener = urllib2.build_opener()
    opener.addheaders = [("User-agent", conf_param["user_agent"]), ("Accept-encoding", "gzip")]

    draw_limit = int(conf_param["num_threads"]) * int(conf_param["over_book"])
    drawQueue(local_queue, draw_limit)

    has_more = True
    iter = 0

    while has_more:
        local_queue.join()
        has_more = drawQueue(local_queue, draw_limit)

        if debug(0):
            printLog("ITER", [iter, has_more])

        if iter >= int(conf_param["max_iterations"]):
            local_queue.join()
            sys.exit(0)
        else:
            iter += 1
            time.sleep(int(conf_param["request_sleep"]))


def visited ():
    ## list the visited URIs

    meta_keys = ["status", "uuid", "crawl_time", "page_len", "domain", "reason"]

    for uri in red_cli.smembers(conf_param["visited_set_key"]):
        meta = red_cli.hmget(uri, meta_keys)
        uri = string.replace(uri, "\n", "_")

        if debug(4):
            printLog("META", [meta])

        status, uuid, crawl_time, page_len, domain, reason = meta

        if status and (len(status) in [3]):
            try:
                print "\t".join([status, uuid, crawl_time, page_len, domain, reason, uri])
            except TypeError, err:
                sys.stderr.write("TypeError: %(err)s\n%(data)s\n" % {"err": str(err), "data": meta})
        else:
            printLog("META ERROR ", meta)


######################################################################
## mapper methods

def extractText (content):
    ## parse the text paragraphs out of HTML content

    para_list = []

    try:
        soup = BeautifulSoup(content)

        # nuke the HTML comments and JavaScript

        comments = soup.findAll(text=lambda text:isinstance(text, Comment))
        c = [comment.extract() for comment in comments] 

        for script in soup.findAll('script'):
            script.extract()

        # get only the text from the <body/>

        if soup.body:
            stone = BeautifulStoneSoup(''.join(soup.body.fetchText(text=True)), convertEntities=BeautifulStoneSoup.ALL_ENTITIES)

            if stone:
                para_list = stone.contents

    except UnicodeEncodeError:
        pass
    except TypeError:
        pass

    return para_list


def parseTerms (para_list):
    ## collect term counts and word bag, out of text paragraphs

    term_count = {}
    word_bag = set([])
    blank_pat = re.compile("^[\-\_]+$")

    for para in para_list:
        try:
            for line in para.split("\n"):
                try:
                    l = re.sub("[^a-z0-9\-\_]", " ", line.strip().lower()).split(" ")

                    for word in l:
                        if not re.search(blank_pat, word) and (len(word) > 0):
                            # term counts within a doc

                            if word not in term_count:
                                term_count[word] = 1
                            else:
                                term_count[word] += 1

                            # "bag of word" (unique terms)
                            word_bag.add(word)

                except ValueError, err:
                    sys.stderr.write("ValueError: %(err)s\n%(data)s\n" % {"err": str(err), "data": str(l)})
        except TypeError:
            pass

    return term_count, word_bag


def getTermList (word_bag):
    ## construct a list of unique words, sorted in alpha order

    term_list = list(map(lambda x: x, word_bag))
    term_list.sort()

    return term_list


def getTermFreq (term_count, term_list):
    ## calculate term frequencies

    sum_tf = float(sum(term_count.values()))
    term_freq = {}

    for i in range(0, len(term_list)):
        word = term_list[i]
        term_freq[word] = float(term_count[word]) / sum_tf

    return term_freq


def emitTerms (uuid, term_list, term_freq, stopwords):
    ## emit co-occurring terms, with pairs in canonical order
    ## (lower triangle of the cross-product)

    for i in range(0, len(term_list)):
        term = term_list[i]

        if not term in stopwords:
            print "\t".join([term, "f", "%.5f" % term_freq[term], uuid])

            for j in range(0, len(term_list)):
                if i != j:
                    co_term = term_list[j]

                    if not co_term in stopwords:
                        print "\t".join([term, "c", co_term])


def mapper (use_queue):
    ## run MapReduce mapper on URIs which need text analytics
    ## TODO: refactor using "yield" to create an iterator instead of "use_queue"

    meta_keys = ["status", "uuid", "crawl_time", "page_len", "domain", "reason"]
    stopwords = red_cli.smembers(conf_param["stop_word_key"])

    if use_queue:
        # draw keys from the "needs_text_key" queue

        while True:
            uri_list = red_cli.lrange(conf_param["needs_text_key"], 0, int(conf_param["map_chunk"]))
            len_uri_list = len(uri_list)

            if len_uri_list < 1:
                break

            red_cli.ltrim(conf_param["needs_text_key"], len_uri_list + 1, -1)

            for uri in uri_list:
                meta = red_cli.hmget(uri, meta_keys) + [uri]
                mapper_sub(meta, stopwords)

    else:
        # draw keys from stdin (for testing only)

        for line in sys.stdin:
            meta = line.strip().split("\t", 6)
            mapper_sub(meta, stopwords)


def mapper_sub (meta, stopwords):
    ## helper function

    status, uuid, crawl_time, page_len, domain, reason, uri = meta

    if status:
        print "\t".join([uri, "m", status, uuid, crawl_time, page_len, domain, reason])

        out_links = red_cli.hget(uri, "out_links")

        if out_links:
            for out_link in resolveLinks(out_links.split("\t")):
                print "\t".join([uri, "l", out_link])

        # TODO currently disabled
        #mapper_text(uri, uuid, stopwords)


def mapper_text (uri, uuid, stopwords):
    ## mapper for text analytics

    term_count, word_bag = parseTerms(extractText(base64.b64decode(red_cli.hget(uri, "html"))))
    term_list = getTermList(word_bag)
    term_freq = getTermFreq(term_count, term_list)

    emitTerms(uuid, term_list, term_freq, stopwords)


if __name__ == "__main__":
    # verify command line usage

    if len(sys.argv) != 3:
        print "Usage: slinky.py host:port:db [ 'config' | 'flush' | 'seed' | 'whitelist' | 'crawl' | 'visited' | 'stopwords' | 'mapper' ] < input.txt"
    else:
        # parse command line options

        red_cli = init(sys.argv[1])
        conf_param = red_cli.hgetall(CONF_PARAM_KEY)
        mode = sys.argv[2]

        if mode == "config":
            # put config params in key/value store, as a distrib cache
            config()
        elif mode == "flush":
            # flush data from key/value store
            flush(True) # False
        elif mode == "seed":
            # seed the URI Queue with root URIs as starting points
            seed()
        elif mode == "whitelist":
            # push white-listed domain rules into key/value store
            putWhitelist()
        elif mode == "crawl":
            # populate local queue and crawl those URIs
            crawl()
        elif mode == "visited":
            # list the visited URIs
            visited()
        elif mode == "stopwords":
            # push stopwords list into key/value store
            putStopwords()
        elif mode == "mapper":
            # run MapReduce mapper on URIs which need text analytics
            mapper(False)
