from __future__ import with_statement
import web
import stopword

from infogami import utils
from infogami.utils import delegate
from infogami.utils import view, template
from infogami import tdb, config
import re
import solr_client
import time
import simplejson
from functools import partial

render = template.render

solr_server_address = getattr(config, 'solr_server_address', None)
solr_fulltext_address = getattr(config, 'solr_fulltext_address',
                                ('ia301443', 8983))

if solr_fulltext_address is not None:
    solr_pagetext_address = getattr(config,
                                    'solr_pagetext_address',
                                    solr_fulltext_address)
    
if solr_server_address:
    solr = solr_client.Solr_client(solr_server_address)
else:
    solr = None

solr_fulltext = solr_client.Solr_client(solr_fulltext_address)
solr_pagetext = solr_client.Solr_client(solr_pagetext_address)

def trans():
    # this should only happen once (or once per long-running thread)
    print >> web.debug, 'loading ocaid translations...'
    with open('id_map') as f:
        d = {}
        for i,x in enumerate(f):
            g = re.match('^([^:]+)_meta.mrc:\d+:\d+ (/b/OL\d+M)$', x)
            a,b = g.group(1,2)
            d[a] = b
    print >> web.debug, len(d), 'translations'
    return d

id_trans = trans()


def lookup_ocaid(ocaid):
    ocat = id_trans.get(ocaid)
    w = web.ctx.site.get(ocat) if ocat is not None else None
    return w

from collapse import collapse_groups
class fullsearch(delegate.page):
    def POST(self):
        errortext = None
        out = []
        q = web.input(q=None).q

        if not q:
            errortext='you need to enter some search terms'
            return render.fullsearch(q, out, errortext)

        try:
            q = re.sub('[\r\n]+', ' ', q).strip()
            results = solr_fulltext.fulltext_search(q)
            for ocaid in results:
                try:
                    pts = solr_pagetext.pagetext_search(ocaid, q)
                    oln_thing = lookup_ocaid(ocaid)
                    if oln_thing is None:
                        # print >> web.debug, 'No oln_thing found for', ocaid
                        pass
                    else:
                        out.append((oln_thing, ocaid,
                                    collapse_groups(solr_pagetext.pagetext_search
                                                    (ocaid, q))))
                except IndexError, e:
                    print >> web.debug, ('fullsearch index error', e, e.args)
                    pass
        except IOError, e:
            errortext = 'fulltext search is temporarily unavailable (%s)' % \
                        str(e)

        return render.fullsearch(q, out, errortext=errortext)

    GET = POST

import facet_hash
facet_token = view.public(facet_hash.facet_token)

class Timestamp(object):
    def __init__(self):
        self.t0 = time.time()
        self.ts = []
    def update(self, msg):
        self.ts.append((msg, time.time()-self.t0))
    def results(self):
        return (time.ctime(self.t0), self.ts)

class search(delegate.page):
    def POST(self):
        i = web.input(wtitle='',
                      wauthor='',
                      wtopic='',
                      wisbn='',
                      wpublisher='',
                      wdescription='',
                      psort_order='',
                      pfulltext='',
                      ftokens=[],
                      q='',
                      )
        timings = Timestamp()
        results = []
        qresults = web.storage(begin=0, total_results=0)
        facets = []
        errortext = None

        if solr is None:
            errortext = 'Solr is not configured.'

        if i.q:
            q0 = [clean_punctuation(i.q)]
        else:
            q0 = []
        for formfield, searchfield in \
                (('wtitle', 'title'),
                 ('wauthor', 'authors'),
                 ('wtopic', 'subjects'),
                 ('wisbn', ['isbn_10', 'isbn_13']),
                 ('wpublisher', 'publishers'),
                 ('wdescription', 'description'),
                 ('pfulltext', 'has_fulltext'),
                 ):
            v = clean_punctuation(i.get(formfield))
            if v:
                if type(searchfield) == str:
                    q0.append('%s:(%s)'% (searchfield, v))
                elif type(searchfield) == list:
                    q0.append('(%s)'% \
                              ' OR '.join(('%s:(%s)'%(s,v))
                                          for s in searchfield))
            # @@
            # @@ need to unpack date range field and sort order here
            # @@
        
        # print >> web.debug, '** i.q=(%s), q0=(%s)'%(i.q, q0)

        # get list of facet tokens by splitting out comma separated
        # tokens, and remove duplicates.  Also remove anything in the
        # initial set `init'.
        def strip_duplicates(seq, init=[]):
            """>>> print strip_duplicates((1,2,3,3,4,9,2,0,3))
            [1, 2, 3, 4, 9, 0]
            >>> print strip_duplicates((1,2,3,3,4,9,2,0,3), [3])
            [1, 2, 4, 9, 0]"""
            fs = set(init)
            return list(t for t in seq if not (t in fs or fs.add(t)))

        # we use multiple tokens fields in the input form so we can support
        # date_range and fulltext_only in advanced search, and can add
        # more like that if needed.
        tokens2 = ','.join(i.ftokens)
        ft_list = strip_duplicates((t for t in tokens2.split(',') if t),
                                   (i.get('remove'),))
        # reassemble ftokens string in case it had duplicates
        i.ftokens = ','.join(ft_list)
        
        # don't throw a backtrace if there's junk tokens.  Robots have
        # been sending them, so just throw away any invalid ones.
        # assert all(re.match('^[a-z]{5,}$', a) for a in ft_list), \
        #       ('invalid facet token(s) in',ft_list)


        ft_list = filter(partial(re.match, '^[a-z]{5,}$'), ft_list)

        qtokens = ' facet_tokens:(%s)'%(' '.join(ft_list)) if ft_list else ''
        ft_pairs = list((t, solr.facet_token_inverse(t)) for t in ft_list)

        # we have somehow gotten some queries for facet tokens with no
        # inverse.  remove these from the list.
        ft_pairs = filter(lambda (a,b): b, ft_pairs)

        if not q0 and not qtokens:
            errortext = 'You need to enter some search terms.'
            return render.advanced_search(i.get('wtitle',''),
                                          qresults,
                                          results,
                                          [],
                                          i.ftokens,
                                          ft_pairs,
                                          [],
                                          errortext=errortext)

        out = []
        i.q = ' '.join(q0)
        try:
            # work around bug in PHP module that makes queries
            # containing stopwords come back empty.
            query = stopword.basic_strip_stopwords(i.q.strip()) + qtokens
            bquery = solr.basic_query(query)
            offset = int(i.get('offset', '0') or 0)
            qresults = solr.advanced_search(bquery, start=offset)
            # qresults = solr.basic_search(query, start=offset)
            timings.update("begin faceting")
            facets = solr.facets(bquery, maxrows=5000)
            timings.update("done faceting")
            results = munch_qresults(qresults.result_list)
            timings.update("done expanding, %d results"% len(results))

        except (solr_client.SolrError, Exception), e:
            import traceback
            errortext = 'Sorry, there was an error in your search.'
            if i.get('safe')=='false':
                errortext +=  '(%r)' % (e.args,)

        # print >> web.debug, 'basic search: about to advanced search (%r)'% \
        #     list((i.get('q', ''),
        #           qresults,
        #           results, 
        #           facets,
        #           i.ftokens,
        #           ft_pairs))


        results = filter(bool, results)

        return render.advanced_search(i.get('q', ''),
                                      qresults,
                                      results, 
                                      facets,
                                      i.ftokens,
                                      ft_pairs,
                                      timings.results(),
                                      errortext=errortext)

    GET = POST

# somehow the leading / got stripped off the book identifiers during some
# part of the search import process.  figure out where that happened and
# fix it later.  for now, just put the slash back.
def restore_slash(book):
    if not book.startswith('/'): return '/'+book
    return book

def munch_qresults(qlist):
    results = []
    rset = set()

    # make a copy of qlist with duplicates removed, but retaining
    # original order
    for res in qlist:
        if res not in rset:
            rset.add(res)
            results.append(res)

    return [web.ctx.site.get(restore_slash(r)) for r in results]

# disable the above function by redefining it as a do-nothing.
# This replaces a version that removed all punctuation from the
# query (see change history, 2008-05-01).  Something a bit smarter
# than this is probably better.
def clean_punctuation(s):
    ws = [w.lstrip(':') for w in s.split()]
    return ' '.join(filter(bool,ws))

class search_api:
    def GET(self):
        def format(val, pp=False):
            if pp:
                return simplejson.dumps(val, indent = 4)
            else:
                return simplejson.dumps(val)
            
        i = web.input(q='{"query":"null"}',
                      rows = 20,
                      offset = 0,
                      prettyprint=False)
        try:
            query = simplejson.loads(i.q)
        except ValueError:
            return format({"status":"error"}, i.prettyprint)
        
        qresult = query and \
                   solr.basic_search(query.get('query').encode('utf8'))
        if not qresult:
            result = []
        else:
            result = qresult.result_list
        dval = { "status": "ok",
                 "result": map(restore_slash, result),
               }
        return format(dval, i.prettyprint)
        
# add search API if api plugin is enabled.
if 'api' in delegate.get_plugins():
    from infogami.plugins.api import code as api
    print "*** adding search api hook"
    api.add_hook('search', search_api)

if __name__ == '__main__':
    import doctest
    doctest.testmod()
