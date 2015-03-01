#!/usr/bin/env python2

from __future__ import print_function

import codecs
import hashlib
import json
import csv
import optparse
import re
import io
import os
import sys
import urllib2
import traceback

from collections import OrderedDict
from contextlib import closing
from ConfigParser import RawConfigParser
from datetime import datetime
from time import time
from pyquery import PyQuery as pq

# The maximum number of records to retrieve from Google Code in a single request
GOOGLE_MAX_RESULTS = 1000

GOOGLE_ISSUES_URL = 'https://code.google.com/p/{}/issues'
GOOGLE_ISSUES_CSV_URL = (GOOGLE_ISSUES_URL +
        '/csv?can=1&num={}&start={}&sort=id&colspec=' +
        '%20'.join([
            'ID',
            'Type',
            'Status',
            'Owner',
            'Summary',
            'Opened',
            'Closed',
            'Reporter',
            'Cc',
        ]))

GOOGLE_ISSUE_PAGE_URL = GOOGLE_ISSUES_URL +'/detail?id={}'

# Format with google_project_name
REF_RE_TMPL = r'''(?x)
    ( (?P<issue>
        (?<![?\-])\b[Ii]s[su]{{2}}e (?=\d*\b) [ \t#-]*
      | (https?://)?code\.google\.com/p/{0}/issues/detail\? )+

    | (?P<commit>
        \b([Rr]ev(ision)?|[Cc]ommit) (?=\d*\b) [ \t#-]*
      | (\br(?=\d\d+\b))  # nobody cares about linking to the first ten commits :(
      | (https?://)?code\.google\.com/p/{0}/source/detail\? )+

    | (?P<link>
        (https?://)?code\.google\.com/p/{0}/source/browse/
        (?P<file> [\w\-\.~%!'"\@/]* ) \?? ) )

    (?P<u>(?<=\?))?
    ( (?(u)[&\w\-=%]*?\b(?(issue)id|r)=)
      (?P<value> (?(issue)\d+|(\d+|\b[0-9a-f]{{7,40}})) )\b (?!=) )?
    (?(u)[&\w\-=%]*)
    (?(file)\#(?P<line>\d+))?
'''

GITHUB_SOURCE_URL = 'https://github.com/{0}/blob'
GITHUB_ISSUES_URL = 'https://github.com/{0}/issues'

GITHUB_SOURCE_PAGE_URL = GITHUB_SOURCE_URL + '/{1}/{2}'  # ref/path
GITHUB_ISSUES_PAGE_URL = GITHUB_ISSUES_URL + '/{1}'      # number

milestones    = OrderedDict()
label_map     = {}
closed_labels = set()
author_map    = {}
commit_map    = {}
messages      = OrderedDict()

class Namespace(object):
    """
    Backport of SimpleNamespace() class added in Python 3.3
    """

    def __init__(self, **kwargs):
        super(Namespace, self).__init__()
        self(**kwargs)

    def __call__(self, **kwargs):
        self.__dict__.update(kwargs)

    __hash__ = None
    def __eq__(self, other):
        return self.__dict__ == other.__dict__

    def __repr__(self):
        keys = sorted(self.__dict__)
        items = ("{}={!r}".format(k, self.__dict__[k]) for k in keys)
        return "{}({})".format(type(self).__name__, ", ".join(items))

class ExtraNamespace(Namespace):
    """
    Adds an extra namespace inaccessible through regular __dict__
    """
    __slots__ = 'extra'

    def __init__(self, **kwargs):
        super(ExtraNamespace, self).__init__()
        self.__dict__.update(kwargs)
        self.extra = Namespace()


def read_json(filename):
    with open(filename, "r") as fp:
        return json.load(fp)

def write_json(obj, filename):
    def namespace_to_dict(obj):
        if isinstance(obj, Namespace):
            return obj.__dict__
        raise TypeError("{} is not JSON serializable".format(obj))

    with open(filename, "w") as fp:
        json.dump(obj, fp, indent=4, separators=(',', ': '), sort_keys=True,
                  default=namespace_to_dict)
        fp.write('\n')

def output(string='', level=0, fp=sys.stdout):
    if options.verbose >= level:
        fp.write(string)
        fp.write('\n')
        fp.flush()


def parse_gcode_date(date_text):
    """ Transforms a Google Code date into a more human readable string. """
    try:
        parsed = datetime.strptime(date_text, '%a %b %d %H:%M:%S %Y').isoformat()
        return parsed + "Z"
    except ValueError:
        return date_text

def timestamp_to_date(timestamp):
    return datetime.fromtimestamp(long(timestamp)).isoformat() + "Z"


def gt(dt_str):
    return datetime.strptime(dt_str.rstrip("Z"), "%Y-%m-%dT%H:%M:%S")


def reindent(s, n=4):
    return "\n".join((n * " ") + i for i in s.splitlines())

def filter_unicode(s):
    for ch in s:
        if ch >= u"\uffff":
            output(" FIXME: unicode %s" % hex(ord(ch)))
            yield "FIXME: unicode %s" % hex(ord(ch))
        else:
            yield ch


###############################################################################

def format_list(lst, fmt='{}', sep=', ', last_sep=None):
    lst = map(fmt.format, lst)
    if last_sep is None or sep == last_sep:
        return sep.join(lst)

    but_tail = sep.join(lst[:-1])
    last_pair = lst[-1:]
    if but_tail:
        last_pair.insert(0, but_tail)


def format_md_user(ns, kind='user'):
    if not kind.startswith('orig_'):
        user = getattr(ns, kind, None)
        if user:
            return '@' + user  # GitHub @mention
        kind = 'orig_' + kind

    if not hasattr(ns, kind):
        ns = ns.extra
    orig_user = getattr(ns, kind)
    return "**{}**{}{}".format(*orig_user.partition('@'))


def format_md_body(paragraphs):
    lines = []
    for title, body in paragraphs:
        for line in title.splitlines():
            lines.append("\n##### " + line)

        if body:
            if "```" not in body:
                body = "```\n" + body + "\n```"
            else:
                output(" FIXME: triple quotes in {} body: {}"
                       .format('issue' if is_issue else 'comment ' + comment_nr,
                               body))
                body = reindent(body)
        lines.append(body)
    return '\n'.join(lines).strip()


def format_md_updates(u):
    lines = []
    emit = lines.append

    if u.orig_owner == '---':
        emit("Unassigned")
    elif u.orig_owner:
        emit("Assigned to {s_owner}")
        s_owner = format_md_user(u, 'owner')

    if u.status in closed_labels:
        if u.close_commit:
            emit("Closed in **{u.close_commit}**")
        else:
            emit("Closed with status **{u.status}**")
    elif u.status:
        emit("Reopened, status set to **{u.status}**")

    if u.mergedinto == '---':
        emit("Unmerged")
    elif u.mergedinto:
        emit("Merged into **#{u.mergedinto}**")

    if u.merged_issue:
        emit("Issue **#{u.merged_issue}** has been merged into this issue")

    if u.old_milestone and u.new_milestone:
        emit("Moved from the **{u.old_milestone}** milestone to **{u.new_milestone}**")
    elif u.old_milestone:
        emit("Removed from the **{u.old_milestone}** milestone")
    elif u.new_milestone:
        emit("Added to the **{u.new_milestone}** milestone")

    s_old_blocking = format_list(u.old_blocking, '**#{}**')
    s_new_blocking = format_list(u.new_blocking, '**#{}**')
    if s_old_blocking:
        emit("No more blocking {s_old_blocking}")
    if s_new_blocking:
        emit("Blocking {s_new_blocking}")

    s_old_blockedon = format_list(u.old_blockedon, '**#{}**')
    s_new_blockedon = format_list(u.new_blockedon, '**#{}**')
    if s_old_blockedon:
        emit("No more blocked on {s_old_blockedon}")
    if s_new_blockedon:
        emit("Blocked on {s_new_blockedon}")

    s_new_labels = format_list(u.new_labels, '**`{}`**')
    s_old_labels = format_list(u.old_labels, '**`{}`**')
    s_labels_plural = 's' * (len(u.new_labels) + len(u.old_labels) > 1)
    if s_new_labels and s_old_labels:
        emit("Added {s_new_labels} and removed {s_old_labels} labels")
    elif s_new_labels:
        emit("Added {s_new_labels} label{s_labels_plural}")
    elif s_old_labels:
        emit("Removed {s_old_labels} label{s_labels_plural}")

    s_new_labels = format_list(u.new_labels, '**`{}`**')
    s_old_labels = format_list(u.old_labels, '**`{}`**')
    s_labels_plural = 's' * (len(u.new_labels) + len(u.old_labels) > 1)
    if s_new_labels and s_old_labels:
        emit("Added {s_new_labels} and removed {s_old_labels} labels")
    elif s_new_labels:
        emit("Added {s_new_labels} label{s_labels_plural}")
    elif s_old_labels:
        emit("Removed {s_old_labels} label{s_labels_plural}")

    return '\n'.join('> {}'.format(line) for line in lines).format(**locals())

def format_markdown(m, comment_nr=0):
    is_issue = (comment_nr == 0)

    header = footer = ''

    if not m.user:
        header = ("<sup>{} by {}</sup>\n"
                  .format('Reported' if is_issue else 'Comment',
                          format_md_user(m, 'orig_user')))

    msg_id = m.extra.link
    try:
        body = messages[msg_id].strip()
    except KeyError:
        body = messages[msg_id] = format_md_body(m.extra.paragraphs)

    if is_issue:
        if m.extra.cc:
            footer += "Cc: {}".format(format_list(m.extra.cc, '@{}'))
        if not m.assignee and m.extra.orig_owner:
            if footer:
                footer += '\n'
            footer += ("> Originally assigned to {s_orig_owner}"
                       .format(s_orig_owner=format_md_user(m, 'orig_owner')))
    else:
        footer = format_md_updates(m.extra.updates)

    if m.extra.attachments:
        a = m.extra.attachments
        if footer:
            footer += '\n>\n'
        footer += ("Attached {s_files} ([view{s_plural} on Gist]({a.url}))"
                   .format(s_files=format_list(a.files.items(),
                                               '[**`{0[0]}`**]({0[1]})'),
                           s_plural=' all' * (len(a.files) > 1), **locals()))


    def gen_msg_blocks():
        if header: yield header
        if body:   yield body
        if footer: yield footer

    return '\n'.join(gen_msg_blocks())

def format_textile(m, comment_nr=0):
    is_issue = (comment_nr == 0)

    i_tmpl = '"#{0}"'
    if options.absolute_links:
        i_tmpl += ':' + GITHUB_ISSUES_URL.format(options.github_repo) + '/{0}'

    body = "bc.. " + m.body + "\n"

    if is_issue:
        body += ("\n" +
                   "p. Original issue for " +
                   i_tmpl.format(m.number) + ": " +
                   '"' + m.extra.link + '":' +
                   m.extra.link + "\n\n" +
                   "p. Original author: " + '"' + m.extra.orig_user +
                   '":' + m.extra.orig_user + "\n")
    if m.extra.refs:
        body += ("\np. Referenced issues: " +
                   ", ".join(i_tmpl.format(i[1:]) for i in m.extra.refs
                             if i.startswith('#')) +
                   "\n")
    if is_issue:
        if m.extra.orig_owner:
            body += ("\np. Original owner: " +
                       '"' + m.extra.orig_owner + '":' + m.extra.orig_owner + "\n")
    else:
        body += ("\np. Original comment: " + '"' + m.extra.link +
                   '":' + m.extra.link + "\n")
        body += ("\np. Original author: " + '"' + m.extra.orig_user +
                   '":' + m.extra.orig_user + "\n")

    return body


MARKDOWN_DATE = gt('2009-04-20T19:00:00Z')

def format_message(m, comment_nr=0):
    # markdown:
    #    http://daringfireball.net/projects/markdown/syntax
    #    http://github.github.com/github-flavored-markdown/
    # vs textile:
    #    http://txstyle.org/article/44/an-overview-of-the-textile-syntax

    if gt(m.created_at) >= MARKDOWN_DATE:
        m.body = format_markdown(m, comment_nr)
    else:
        m.body = format_textile(m, comment_nr)

    if len(m.body) >= 65534:
        m.body = "FIXME: too long issue body"
        output(" FIXME: too long {} body"
               .format('issue' if is_issue else 'comment '+comment_nr))


def add_issue_to_github(issue):
    """ Migrates the given Google Code issue to Github. """
    output('Exporting issue {}'.format(issue.number), level=1)

    format_message(issue)
    write_json(issue, "issues/{}.json".format(issue.number))

    for i, comment in enumerate(issue.extra.comments):
        format_message(comment, i+1)
    write_json(issue.extra.comments, "issues/{}.comments.json".format(issue.number))


###############################################################################

def split_into_paragraphs(pquery, title_selector='b'):
    paragraphs = []

    was_title = None
    title = ''
    accum_text = ''

    for paragraph in pquery.contents():
        is_str = isinstance(paragraph, basestring)
        text = (paragraph if is_str else (paragraph.text or '').strip())
        if not text:
            continue
        is_title = not is_str and pq(paragraph).is_(title_selector)
        if is_title == was_title:
            accum_text += text
            continue
        if was_title is not None:
            accum_text = accum_text.strip()
            if was_title:
                title = accum_text
            else:
                paragraphs.append((title, accum_text))
        accum_text = text
        was_title = is_title
    else:
        if was_title is not None:
            accum_text = accum_text.strip()
            if was_title:
                title = accum_text
                accum_text = ''
            paragraphs.append((title, accum_text))

    return paragraphs

def join_paragraphs(paragraphs):
    return '\n\n'.join(title + '\n' + body for title, body in paragraphs).strip()


def map_author(gc_uid, kind=None, fallback=True):
    email_pat = gc_uid
    if '@' not in email_pat:
        email_pat += '@gmail.com'
    email_pat = re.escape(email_pat).replace(r'\.\.\.\@', r'[\w.]+\@')
    email_re = re.compile(email_pat, re.I)

    matches = []
    for email, gh_user in author_map.items():
        if email_re.match(email):
            matches.append((email, gh_user))
    if len(matches) > 1:
        output('FIXME: multiple matches for {gc_uid}'.format(**locals()))
        for email, gh_user in matches:
            output('\t{email}'.format(**locals()))
    elif matches:
        output("{:<10}    {:>22} -> {:>32}:   {:<16}"
               .format(kind, gc_uid, *matches[0]), level=3)
        return matches[0][1]

    output("{:<10}!!! {:>22}".format(kind, gc_uid), level=2)

    if fallback:
        return options.fallback_user


def fixup_refs(s, add_ref=None):
    def fix_ref(match):
        ref = None

        value = match.group('value')
        if value or match.group('link'):

            if match.group('issue'):
                ref = '#' + str(int(value) + (options.issues_start_from - 1))
            elif value and commit_map:
                try:
                    ref = commit_map[value]
                except KeyError:
                    output('FIXME: rev {} not found in commit map'.format(value))

            filename = match.group('file')
            if filename:
                branch = 'master'
                pathfrags = filename.split('/')
                if pathfrags[0] == 'trunk':
                    del pathfrags[0]
                    if pathfrags and pathfrags[0] == 'embox':
                        del pathfrags[0]

                elif pathfrags[0] in ('branches', 'tags') and len(pathfrags) > 1:
                    branch = pathfrags[1]
                    del pathfrags[:1]
                filename = '/'.join(pathfrags)

                ref = (GITHUB_SOURCE_PAGE_URL
                       .format(options.github_repo, ref or branch, filename))

        if not ref:
            return match.group()

        if add_ref is not None:
            add_ref(ref)

        output('>>> {:>50}  {}'.format(ref, match.group()), level=3)
        return ref

    return re.sub(REF_RE_TMPL.format(google_project_name), fix_ref, s)


def init_attachments(m, pquery):
    attachments = attachments_cache.get(m.extra.link)
    if attachments:
        m.extra.attachments = Namespace(**attachments)
        output('Gist attachments URL (from cache): {}'
               .format(m.extra.attachments.url), level=1)
        return
    # else:
    m.extra.attachments = None

    files = OrderedDict()
    for attachment_pq in pquery('.attachments > table').items():
        for link in attachment_pq('a').items():
            if link.text() == 'Download':
                break
        else:
            continue

        attachment_name = attachment_pq('b').text()
        attachment_url = link.attr('href')

        output("Downloading attachment '{}' "
               .format(attachment_name), level=2)
        try:
            with closing(urllib2.urlopen(attachment_url)) as sf:
                content = sf.read()
        except urllib2.URLError:
            output("FIXME: Unable to get an attachment file '{}' from '{}'"
                   .format(attachment_name, attachment_url))
            continue

        try:
            files[attachment_name] = {'content': content.decode('utf-8')}
        except UnicodeDecodeError:
            output("Skipping binary file", level=2)

    if files:
        data = {'description': (('Issue attachments for {0}#{1}: ' +
                                 GITHUB_ISSUES_PAGE_URL)
                                .format(options.github_repo, m.extra.issue_number)),
                'files': files, 'public': False}

        request = urllib2.Request('https://api.github.com/gists', json.dumps(data),
                                  {'Content-Type': 'application/json'})

        try:
            with closing(urllib2.urlopen(request)) as sf:
                response = json.load(sf, object_pairs_hook=OrderedDict)
        except urllib2.URLError:
            output("FIXME: Unable to post attachments to Gist"
                   .format(attachment_name, attachment_url))
        else:
            m.extra.attachments = Namespace(
                url=response['html_url'],
                files=OrderedDict((name, obj['raw_url'])
                                  for name, obj in response['files'].items()))
            output('Gist attachments URL: {}'
                   .format(m.extra.attachments.url), level=1)

            attachments_cache[m.extra.link] = m.extra.attachments


def init_message(m, pquery):
    refs = set()
    paragraphs = [tuple(fixup_refs(text, add_ref=refs.add) for text in pair)
                  for pair in split_into_paragraphs(pquery('pre'))]

    # Strip the placeholder text, if any
    if len(paragraphs) == 1 and hasattr(m.extra, 'updates'):
        body = paragraphs[0][1]
        if body == '(No comment was entered for this change.)':
            del paragraphs[0]

        if body.startswith('Set review issue status to:'):
            del paragraphs[0]

        close_commit = re.match(r'This issue was closed by ([0-9a-f]+)\.', body)
        if close_commit:
            m.extra.updates.close_commit = close_commit.group(1)
            del paragraphs[0]

        merged_issue = re.match(r'#(\d+) has been merged into this issue\.', body)
        if merged_issue:
            m.extra.updates.merged_issue = merged_issue.group(1)
            del paragraphs[0]

    if (len(paragraphs) == 1 and
        paragraphs[0][1] == '(No comment was entered for this change.)'):
        del paragraphs[0]

    m.extra.refs = refs
    m.extra.paragraphs = paragraphs
    m.body = join_paragraphs(paragraphs)

    init_attachments(m, pquery)


def add_label_or_milestone(label, labels_to_add):
    milestone = get_or_create_milestone(label)
    if milestone:
        return milestone

    label = label_map.get(label, label)
    if label and label not in labels_to_add:
        labels_to_add.append(label)


def get_gcode_updates(updates_pq):
    updates = Namespace(
        orig_owner    = None,
        owner         = None,
        status        = None,
        mergedinto    = None,
        new_milestone = None,
        old_milestone = None,
        new_blockedon = [],
        old_blockedon = [],
        new_blocking  = [],
        old_blocking  = [],
        new_labels    = [],
        old_labels    = [],
        merged_issue  = None,
        close_commit  = None)

    for key, value in split_into_paragraphs(updates_pq):
        key = key.partition(':')[0]

        if key in ('Blockedon', 'Blocking', 'Labels'):
            new_lst = updates.__dict__['new_'+key.lower()]
            old_lst = updates.__dict__['old_'+key.lower()]

            for word in value.split():
                is_removed = word.startswith('-')
                if is_removed:
                    word = word[1:]
                lst = (old_lst if is_removed else new_lst)

                if key in ('Blockedon', 'Blocking'):
                    ref_text = word.rpartition(':')[-1]
                    ref = int(ref_text) + (options.issues_start_from - 1)
                    if ref not in lst:
                        lst.append(ref)

                elif key == 'Labels':
                    milestone = add_label_or_milestone(word, lst)
                    if milestone:
                        if is_removed:
                            updates.old_milestone = milestone.title
                        else:
                            updates.new_milestone = milestone.title

            for el in set(old_lst) & set(new_lst):
                old_lst.remove(el)

        if key == 'Owner':
            updates.orig_owner = value
            updates.owner = map_author(value, 'owner')

        elif key == 'Status':
            updates.status = value

        elif key == 'Mergedinto':
            ref_text = value.rpartition(':')[-1]
            if ref_text:
                updates.mergedinto = int(ref_text) + (options.issues_start_from - 1)
            else:
                updates.mergedinto = '---'

    return updates


def get_gcode_comment(issue, comment_pq):
    comment = ExtraNamespace(
        created_at = parse_gcode_date(comment_pq('.date').attr('title')),
        updated_at = options.export_date)

    comment.extra.issue_number = issue.number
    comment.extra.link = issue.extra.link + '#' + comment_pq('a').attr('name')
    comment.extra.updates = get_gcode_updates(comment_pq('.updates .box-inner'))

    init_message(comment, comment_pq)

    paragraphs = comment.extra.paragraphs
    if len(paragraphs) > 1 or paragraphs and paragraphs[0][0]:
        output("FIXME: unexpected paragraph structure in {}"
               .format(comment.link))

    comment.extra.orig_user  = comment_pq('.userlink').text()
    comment.user = map_author(comment.extra.orig_user, 'comment')

    return comment


def get_gcode_issue(summary):
    output('Importing issue {}'.format(int(summary['ID'])), level=1)

    # Populate properties available from the summary CSV
    issue = ExtraNamespace(
        number     = int(summary['ID']) + (options.issues_start_from - 1),
        title      = summary['Summary'].replace('%', '&#37;').strip(),
        state      = 'closed' if summary['Closed'] else 'open',
        closed_at  = timestamp_to_date(summary['ClosedTimestamp']) if summary['Closed'] else None,
        created_at = timestamp_to_date(summary['OpenedTimestamp']),
        updated_at = options.export_date)

    if not issue.title:
        issue.title = "FIXME: empty title"
        output(" FIXME: empty title")

    issue.extra.issue_number = issue.number

    issue.extra.orig_user = summary['Reporter']
    issue.user = map_author(issue.extra.orig_user, 'reporter')

    issue.extra.orig_owner = summary['Owner']
    if issue.extra.orig_owner:
        issue.assignee = map_author(issue.extra.orig_owner, 'owner')
    else:
        issue.assignee = None

    issue.extra.cc = filter(None, (map_author(cc, 'cc', fallback=False)
                                   for cc in filter(None, summary['Cc'].split(', '))))

    issue.extra.link = GOOGLE_ISSUE_PAGE_URL.format(google_project_name, summary['ID'])

    # Build a list of labels to apply to the new issue, including an 'imported' tag that
    # we can use to identify this issue as one that's passed through migration.
    issue.labels = []
    if options.imported_label:
        issue.labels.append(options.imported_label)

    for label in filter(None, summary['AllLabels'].split(', ')) + [summary['Status']]:
        milestone = add_label_or_milestone(label, issue.labels)
        if milestone:
            if not hasattr(milestone, 'created_at'):
                milestone.created_at = issue.created_at
            if issue.state == 'open':
                milestone.state = 'open'
            issue.milestone = milestone.number

    # Scrape the issue details page for the issue body and comments
    doc = pq(issue.extra.link)
    doc.make_links_absolute()

    issue_pq = doc('.issuedescription .issuedescription')

    init_message(issue, issue_pq)

    issue.extra.comments = []
    for comment_pq in map(pq, doc('.issuecomment')):
        if not comment_pq('.date'):
            continue # Sign in prompt line uses same class
        if comment_pq.hasClass('delcom'):
            continue # Skip deleted comments

        comment = get_gcode_comment(issue, comment_pq)
        issue.extra.comments.append(comment)

    return issue

def get_gcode_issues():
    issues = []
    while True:
        url = GOOGLE_ISSUES_CSV_URL.format(google_project_name,
                                           GOOGLE_MAX_RESULTS, len(issues))
        issues.extend(csv.DictReader(urllib2.urlopen(url), dialect=csv.excel))

        if issues and 'truncated' in issues[-1]['ID']:
            issues.pop()
        else:
            break

    output('Fetched summaries for {} issues'.format(len(issues)))
    return issues


def process_gcode_issues():
    """ Migrates all Google Code issues in the given dictionary to Github. """

    issues = get_gcode_issues()
    previous_gid = 1

    if options.start_at is not None:
        issues = [x for x in issues if int(x['ID']) >= options.start_at]
        previous_gid = options.start_at - 1
        output('Starting at issue {}'.format(options.start_at), level=1)

    if options.end_at is not None:
        issues = [x for x in issues if int(x['ID']) <= options.end_at]
        output('End at issue {}'.format(options.end_at), level=1)

    for summary in issues:
        issue = get_gcode_issue(summary)

        if options.skip_closed and (issue.state == 'closed'):
            continue

        add_issue_to_github(issue)

    if milestones:
        for m in milestones.values():
            output('Adding milestone {}'.format(m.number), level=1)
            write_json(m, 'milestones/{}.json'.format(m.number))


def get_or_create_milestone(label, warn_duplicate=False):
    global milestones

    kind, _, value = label.partition('-')
    if kind != options.milestone_label_prefix:
        return

    if not value:
        output("FIXME: Unable to parse milestone name: '{}'".format(label))
        return

    try:
        milestone = milestones[value]
    except KeyError:
        milestone = milestones[value] = Namespace(
           number = len(milestones) + options.milestones_start_from,
           title  = value)
    else:
        if warn_duplicate:
            output("Warning: Duplicate milestone: '{}'".format(value))

    return milestone


def extract_milestone_labels(label_map):
    for label, description in label_map.items():
        milestone = get_or_create_milestone(label, warn_duplicate=True)
        if not milestone:
            if len(description.split()) > 1:
                output("Warning: Non-singleword GitHub issue label: '{}'"
                       .format(description))
            continue

        del label_map[label]

        milestone.state  = 'closed',  # unless there will be any open issues encountered

        date_match = re.match(r'^\[([^\]]+)\]\s*', description)
        if date_match:
            date_text = date_match.group(1)
            parsed_date = datetime.strptime(date_text, options.milestone_label_date_format)

            milestone.due_on = parsed_date.isoformat() + "Z"

            description = description[date_match.end():]

        if description:
            milestone.description = description


def config_section(config, section_name):
    section = OrderedDict()
    for option in config.options(section_name):
        section[option] = config.get(section_name, option)
    return section

def read_ini(filename):
    config = RawConfigParser()
    config.optionxform = str

    config.read(filename)

    sections = OrderedDict()

    for section_name in config.sections():
        sections[section_name] = config_section(config, section_name)

    return sections

def read_messages(filename):
    messages = OrderedDict()

    msg_id = None
    with codecs.open(filename, "r", encoding='utf-8') as f:
        for line in f:
            frags = line.split(None, 4)
            if len(frags) == 4:
                start, mb_msg_id, checksum, end = frags
                if (start == '<!--' and end == '-->' and
                    checksum == hashlib.md5(mb_msg_id).hexdigest()):
                    msg_id = mb_msg_id
                    continue

            messages[msg_id] = messages.get(msg_id, '') + line
        else:
            messages.setdefault(msg_id, '')
    messages.pop(None, None)

    output("Read {} overrides from {}".format(len(messages), filename))

    return messages

def write_messages(messages, filename):
    try:
        os.rename(filename, filename + "-old")
    except OSError:
        pass

    with codecs.open(filename, "w", encoding='utf-8') as f:
        for msg_id, body in messages.items():
            f.write('<!--  {}   {}  -->\n'
                    .format(msg_id, hashlib.md5(msg_id).hexdigest()))
            f.write(body.strip())
            f.write('\n\n')


# Reasonable defaults for config options.
CONFIG_DEFAULT_INI = """
[google]
project
start-at
end-at
skip-closed = false

[github]
repo
fallback-user
absolute-links = false
issues-start-from     = 1
milestones-start-from = 1
export-date = {now}

[include]
authors-json
labels-ini
commits-maps =
messages-input
messages-output

[misc]
imported-label = imported
milestone-label-prefix = Milestone
milestone-label-date-format = %Y-%m-%d
cache-attachments = true

""".format(now=datetime.now().isoformat() + "Z")


def main():
    global options, google_project_name
    global milestones
    global author_map
    global closed_labels
    global label_map
    global commit_map
    global messages
    global attachments_cache

    config = RawConfigParser(allow_no_value=True)
    config.optionxform = str

    config.readfp(io.BytesIO(CONFIG_DEFAULT_INI))
    config.read('config.ini')

    parser = optparse.OptionParser(
            usage="usage: %prog [options] [<google-project>]",
            description="Export all issues from a Google Code project for a GitHub repo.")

    google = optparse.OptionGroup(parser, title="Google Code options")

    google.add_option('--start-at', type=int,
            default=config.get('google', 'start-at'),
            help='Start at the given Google Code issue number')
    google.add_option('--end-at', type=int,
            default=config.get('google', 'end-at'),
            help='End at the given Google Code issue number')
    google.add_option('--skip-closed', action='store_true',
            default=config.getboolean('google', 'skip-closed'),
            help='Skip all closed bugs')

    parser.add_option_group(google)


    github = optparse.OptionGroup(parser, title="GitHub options")

    github.add_option('--github-repo',
            default=config.get('github', 'repo'),
            help='Used to construct URLs if --absolute-links is given')
    github.add_option('--fallback-user',
            default=config.get('github', 'fallback-user'),
            help='Default username (e.g. bot account) to use for unknown users')
    github.add_option('--absolute-links', action='store_true',
            default=config.getboolean('github', 'absolute-links'),
            help='Absolute URLs in links to issues and source files')

    github.add_option('--issues-start-from', type=int,
            default=config.get('github', 'issues-start-from'),
            help='First issue number')
    github.add_option('--milestones-start-from', type=int,
            default=config.get('github', 'milestones-start-from'),
            help='First milestone number')

    github.add_option('--export-date',
            default=config.get('github', 'export-date'),
            help='Date of export')

    parser.add_option_group(github)


    include = optparse.OptionGroup(parser, title="Included files")

    include.add_option('--authors-json',
            default=config.get('include', 'authors-json'),
            help='Mapping of Google Code emails to GitHub usernames')
    include.add_option('--labels-ini',
            default=config.get('include', 'labels-ini'),
            help='Mapping of Google Code labels to GitHub counterparts')
    include.add_option('--commits-map', action='append',
            default=[f.strip() for f in config.get('include', 'commits-maps').split(',')
                     if f.strip()],
            help='Map file(s) for revision references')

    include.add_option('--messages-output',
            default=config.get('include', 'messages-output'),
            help='Dump messages text into a given file used')
    include.add_option('--messages-input',
            default=config.get('include', 'messages-input'),
            help='Override certain messages with a text taken from a given file')

    parser.add_option_group(include)


    misc = optparse.OptionGroup(parser, title="Misc options")

    misc.add_option('--imported-label',
            default=config.get('misc', 'imported-label'),
            help='A label to mark all imported issues')

    misc.add_option('--milestone-label-prefix',
            default=config.get('misc', 'milestone-label-prefix'),
            help='Label prefix to recognize milestones')
    misc.add_option('--milestone-label-date-format',
            default=config.get('misc', 'milestone-label-date-format'),
            help='Format of [date] for milestones taken from the labels config')

    misc.add_option('--no-cache-attachments',
            action='store_false', dest='cache_attachments',
            default=config.get('misc', 'cache-attachments'),
            help='Download all attachments and create new Gists from scratch')

    parser.add_option_group(misc)


    parser.add_option('-v', '--verbose', action='count', default=0,
            help='Verbosity level (-v to -vvv)')


    options, args = parser.parse_args()

    if len(args) > 1:
        parser.print_help()
        sys.exit(1)

    if args:
        google_project_name = args[0]
    elif config.get('google', 'project'):
        google_project_name = config.get('google', 'project')
    else:
        output("Error: No Google Code project name given")
        parser.print_help()
        sys.exit(1)

    if not options.github_repo:
        options.github_repo = '{0}/{0}'.format(google_project_name)
        if options.absolute_links:  # otherwise unused
            output("Warning: GitHub repo name is set to '{}'"
                   .format(options.github_repo))

    if options.authors_json:
        author_map.update(read_json(options.authors_json))

    if options.labels_ini:
        labels_config = read_ini(options.labels_ini)

        for section in 'open', 'closed', 'labels':
            label_map.update(labels_config.get(section, {}))
        closed_labels.update(labels_config.get('closed', {}))

        extract_milestone_labels(label_map)

    for map_filename in reversed(options.commits_map):
        tmp_map = commit_map
        commit_map = {}
        with open(map_filename, 'r') as f:
            for line in f:
                if not line.strip():
                    continue
                key, value = (s.strip() for s in line.split(None, 1))

                commit_map[key] = tmp_map[value] if tmp_map else value

    if not os.path.exists('issues'):
        os.mkdir('issues')

    if not os.path.exists('milestones'):
        os.mkdir('milestones')

    if options.messages_input:
        messages = read_messages(options.messages_input)

    if options.cache_attachments:
        try:
            attachments_cache = read_json('.attachments-cache.json')
        except IOError:
            attachments_cache = {}

    try:
        process_gcode_issues()
    except Exception:
        output()
        parser.print_help()
        raise
    finally:
        try:
            write_json(attachments_cache, '.attachments-cache.json')
        except IOError:
            output("Warning: unable to save attachments cache")

    if options.messages_output:
        write_messages(messages, options.messages_output)

if __name__ == "__main__":
    main()
