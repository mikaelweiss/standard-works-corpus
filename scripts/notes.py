#!/usr/bin/env python3
# Writes study notes into the summary table, one agent job at a time.
# A job covers a chapter, a verse range, or a book (split by kind when the
# book's chapter notes would not fit in one prompt). Each job writes every
# kind for its target at once, so the kinds stay distinct.
#
# Usage:
#   notes.py plan             batches of jobs not yet applied, as JSON
#   notes.py prompt <job>     writes the job's prompt and prints where to answer
#   notes.py apply <job>      checks the answer and loads it into summary
#   notes.py status           jobs applied so far
import json
import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATABASE = ROOT / 'scripture.db'
JOBS = ROOT / 'jobs' / 'notes'

EM_DASH = chr(0x2014)
ENGLISH_EDITIONS = (1, 4, 5, 6)
ORIGINAL_PARTS = ('Noun', 'Verb', 'Adjective', 'Adverb', 'Proper noun')
UNNUMBERED_BOOKS = {'Facsimile 1', 'Facsimile 2', 'Facsimile 3', 'Title Page', 'Testimony of Three Witnesses', 'Testimony of Eight Witnesses'}
ALIASES = {'D&C': 'Doctrine and Covenants', 'Psalm': 'Psalms'}

PASSAGE_WORDS = (15, 120)
BOOK_WORDS = (50, 250)
# A chapter note averages about this many words, which sizes book prompts.
NOTE_WORDS = 60
BOOK_PROMPT_WORDS = 30000
CHAPTER_BATCH = (6, 5000)
RANGE_BATCH = (6, 8000)
BOOK_BATCH = (6, 30000)

SENTENCE_END = re.compile(r'[.?!]["\'”’)]*$')
LIST_MARKER = re.compile(r'(^|\s)([-*•]|#+|\d+[.)])\s')

INSTRUCTIONS = """# Study notes

You write study notes on scripture for personal study. Each target under This job (a chapter, a passage, or a book) gets one note of each kind listed for it, so a reader can skim a whole book through one kind.

- Write from a Latter-day Saint perspective, in plain modern English and full sentences. Capitalize pronouns for God: "His commandments".
- {length} Write prose: no lists, headings, markdown, or line breaks.
- Each note covers only its own kind, and only what its target holds. Keep the kinds distinct: never make the same point under Doctrine, Principles, and Commandments.
- Name verses the way people write them, with the book's full name: 1 Nephi 3:7, Psalms 23:4, Joseph Smith{dash}History 1:17. For the Doctrine and Covenants write D&C 89:3. For a facsimile write Facsimile 2, Figure 3.
- Write from what this prompt shows. The speeches say who is talking to whom. The people and places list tells apart people who share a name. For the Bible, the original words list gives the Hebrew or Greek behind the English. Where a kind needs background the text does not give, such as setting or culture, use only well-established facts and leave out anything you are unsure of.
- Never copy or paraphrase the Church's chapter summaries, section headings, footnotes, or other study helps, even from memory. Write in your own words, quoting the text only briefly.
- When a target holds nothing of a kind, such as no prophecy at all, give that kind an empty string rather than stretching. Key verses is never empty.

## Kinds

{kinds}

## Answer

Write one JSON object to {answer}: the target's name as This job gives it, mapped to an object of every kind listed above.

{example}
"""


def connect():
    db = sqlite3.connect(DATABASE, timeout=300, isolation_level=None)
    db.execute('pragma foreign_keys = on')
    return db


class Corpus:
    def __init__(self, db):
        self.db = db
        self.books = {row[0]: {'name': row[1], 'work': row[2]} for row in db.execute('select id, name, work_id from book')}
        self.book_ids = {book['name']: id for id, book in self.books.items()}
        self.positions = dict(db.execute(f'select book_id, work_id * 1000 + position from edition_book where edition_id in {ENGLISH_EDITIONS}'))
        self.chapters = {id: (book, number) for id, book, number in db.execute(f'select id, book_id, number from chapter where edition_id in {ENGLISH_EDITIONS}')}
        self.book_chapters = {}
        for id, (book, number) in sorted(self.chapters.items(), key=lambda item: (self.positions[item[1][0]], item[1][1])):
            self.book_chapters.setdefault(book, []).append(id)
        self.verses = set(db.execute(
            f'select c.book_id, c.number, v.number from verse v join chapter c on c.id = v.chapter_id where c.edition_id in {ENGLISH_EDITIONS} and v.number is not null'))
        self.kinds = {id: (name, description) for id, name, description in db.execute('select id, name, description from summary_kind order by id')}
        self.work_kinds = {}
        for kind, work in db.execute('select summary_kind_id, work_id from summary_kind_work order by summary_kind_id'):
            self.work_kinds.setdefault(work, []).append(kind)
        names = sorted(list(self.book_ids) + list(ALIASES), key=len, reverse=True)
        self.reference = re.compile(r'(?<![\w])(' + '|'.join(map(re.escape, names)) + r') (\d+):(\d+)(?:[–-](\d+))?')
        self.unknown_reference = re.compile(r'(?<![\w])((?:\d )?[A-Z][\w&.]*(?:[ ' + EM_DASH + r'-][A-Z][\w&.]*)*) (\d+):(\d+)')

    def chapter_label(self, chapter):
        book, number = self.chapters[chapter]
        name = self.books[book]['name']
        return name if name in UNNUMBERED_BOOKS else f'{name} {number}'

    def book_name(self, book):
        return self.books[book]['name']

    def chapter_words(self, chapter):
        return self.db.execute('select count(*) from word w join verse v on v.id = w.verse_id where v.chapter_id = ?', (chapter,)).fetchone()[0]

    def range_row(self, range_id):
        return self.db.execute(
            'select r.name, a.sequence, b.sequence from verse_range r join word a on a.id = r.first_word_id join word b on b.id = r.last_word_id where r.id = ?',
            (range_id,)).fetchone()

    def place(self, sequence):
        """The (book, chapter number, verse number) holding a word."""
        return self.db.execute(
            'select c.book_id, c.number, v.number from word w join verse v on v.id = w.verse_id join chapter c on c.id = v.chapter_id where w.sequence = ?',
            (sequence,)).fetchone()

    def range_work(self, range_id):
        return self.books[self.place(self.range_row(range_id)[1])[0]]['work']

    def range_span(self, range_id):
        """The range's verses as people write them, such as Mosiah 2:9–5:15."""
        _, first, last = self.range_row(range_id)
        (b1, c1, v1), (b2, c2, v2) = self.place(first), self.place(last)
        start = f'{self.book_name(b1)} {c1}:{v1 or 1}'
        if (b1, c1) == (b2, c2):
            return start if v1 == v2 else f'{start}–{v2}'
        if b1 == b2:
            return f'{start}–{c2}:{v2}'
        return f'{start}–{self.book_name(b2)} {c2}:{v2}'

    def range_words(self, range_id):
        _, first, last = self.range_row(range_id)
        return last - first + 1


def book_kind_groups(corpus, book):
    kinds = corpus.work_kinds[corpus.books[book]['work']]
    size = max(1, BOOK_PROMPT_WORDS // (NOTE_WORDS * len(corpus.book_chapters[book])))
    return [kinds[start:start + size] for start in range(0, len(kinds), size)]


def all_jobs(corpus):
    chapters = [f'chapter-{chapter}' for book in corpus.book_chapters for chapter in corpus.book_chapters[book]]
    ranges = [f'range-{id}' for id, in corpus.db.execute(
        'select r.id from verse_range r join word w on w.id = r.first_word_id order by w.sequence')]
    books = []
    for book in corpus.book_chapters:
        groups = book_kind_groups(corpus, book)
        books += [f'book-{book}'] if len(groups) == 1 else [f'book-{book}-{part}' for part in range(1, len(groups) + 1)]
    return chapters, ranges, books


def parse_job(job):
    parts = job.split('-')
    if parts[0] not in ('chapter', 'range', 'book') or len(parts) not in (2, 3) or not all(part.isdigit() for part in parts[1:]):
        raise SystemExit(f'no such job: {job}')
    return parts[0], int(parts[1]), int(parts[2]) if len(parts) == 3 else None


def job_target(corpus, job):
    """The job's target as (label, summary column, id, kinds, word limits)."""
    kind, id, part = parse_job(job)
    if kind == 'chapter':
        if id not in corpus.chapters:
            raise SystemExit(f'no English chapter {id}')
        work = corpus.books[corpus.chapters[id][0]]['work']
        return corpus.chapter_label(id), 'chapter_id', id, corpus.work_kinds[work], PASSAGE_WORDS
    if kind == 'range':
        return corpus.range_row(id)[0], 'verse_range_id', id, corpus.work_kinds[corpus.range_work(id)], PASSAGE_WORDS
    return corpus.book_name(id), 'book_id', id, book_kind_groups(corpus, id)[(part or 1) - 1], BOOK_WORDS


def is_applied(job):
    return (JOBS / job / 'applied').exists()


def batches(jobs, limit, size):
    out, current, total = [], [], 0
    for job in jobs:
        if is_applied(job):
            continue
        weight = size(job)
        if current and (len(current) >= limit[0] or total + weight > limit[1]):
            out.append(current)
            current, total = [], 0
        current.append(job)
        total += weight
    if current:
        out.append(current)
    return out


def plan(corpus):
    _, ranges, books = all_jobs(corpus)

    def book_size(job):
        _, book, part = parse_job(job)
        return len(corpus.book_chapters[book]) * len(book_kind_groups(corpus, book)[(part or 1) - 1]) * NOTE_WORDS

    chapters = []
    for book, ids in corpus.book_chapters.items():
        chapters += batches([f'chapter-{id}' for id in ids], CHAPTER_BATCH, lambda job: corpus.chapter_words(parse_job(job)[1]))
    return {
        'chapters': chapters,
        'ranges': batches(ranges, RANGE_BATCH, lambda job: corpus.range_words(parse_job(job)[1])),
        'books': batches(books, BOOK_BATCH, book_size),
    }


def verse_text(db, first, last):
    """Whole verses holding the words from sequence first to last, as [chapter id, verse number, text]."""
    rows = db.execute(
        '''select v.id, v.chapter_id, v.number, w.before || w.text || w.after from word w join verse v on v.id = w.verse_id
           where v.id in (select verse_id from word where sequence between ? and ?)
           order by w.sequence''', (first, last))
    verses = {}
    for verse, chapter, number, text in rows:
        verses.setdefault(verse, [chapter, number, ''])[2] += text
    return list(verses.values())


def render_text(corpus, first, last):
    lines, chapter = [], None
    for chapter_id, number, text in verse_text(corpus.db, first, last):
        if chapter_id != chapter:
            chapter = chapter_id
            lines.append(f'\n### {corpus.chapter_label(chapter_id)}\n')
        lines.append(f'{number} {text.strip()}' if number is not None else f'(heading) {text.strip()}')
    return '\n'.join(lines).strip()


def span_label(corpus, first, last):
    (_, c1, v1), (_, c2, v2) = corpus.place(first), corpus.place(last)
    v1, v2 = v1 or 'heading', v2 or 'heading'
    if c1 != c2:
        return f'{c1}:{v1}–{c2}:{v2}'
    return f'{c1}:{v1}' if v1 == v2 else f'{c1}:{v1}–{v2}'


def render_speeches(corpus, first, last):
    db = corpus.db
    speeches = db.execute(
        '''select s.id, m.name, a.sequence, b.sequence, (select name from entity where id = s.through_id)
           from speech s join speech_mode m on m.id = s.speech_mode_id
           join word a on a.id = s.first_word_id join word b on b.id = s.last_word_id
           where a.sequence <= ? and b.sequence >= ? order by a.sequence, b.sequence desc''', (last, first)).fetchall()
    lines = []
    for id, mode, start, end, through in speeches:
        speakers = [row[0] for row in db.execute('select e.name from speech_speaker s join entity e on e.id = s.entity_id where s.speech_id = ? order by e.name', (id,))]
        listeners = [row[0] for row in db.execute('select e.name from speech_listener s join entity e on e.id = s.entity_id where s.speech_id = ? order by e.name', (id,))]
        line = f'- {span_label(corpus, max(start, first), min(end, last))} {mode}: {", ".join(speakers) or "unknown"}'
        if listeners:
            line += f' to {", ".join(listeners)}'
        if through:
            line += f', through {through}'
        lines.append(line)
    return '\n'.join(lines) or 'None tagged.'


def render_entities(corpus, first, last):
    rows = corpus.db.execute(
        '''select e.name, t.name, e.description, count(*) from mention m
           join word a on a.id = m.first_word_id join entity e on e.id = m.entity_id
           join entity_type t on t.id = e.entity_type_id join mention_kind k on k.id = m.mention_kind_id
           where a.sequence between ? and ? and k.name = 'Refers to'
           group by e.id order by min(a.sequence)''', (first, last))
    return '\n'.join(f'- {name} ({kind}, {count}x): {description}' for name, kind, description, count in rows) or 'None tagged.'


def render_original(corpus, first, last):
    rows = corpus.db.execute(
        f'''select h.text, h.strongs, coalesce(m.gloss, h.gloss), group_concat(distinct w.text), group_concat(distinct v.number)
            from word w join verse v on v.id = w.verse_id
            join word_match x on x.word_id = w.id join word o on o.id = x.other_word_id
            join headword h on h.id = o.headword_id join part_of_speech p on p.id = o.part_of_speech_id
            left join meaning m on m.headword_id = o.headword_id and m.id = o.meaning_id
            where w.sequence between ? and ? and p.name in {ORIGINAL_PARTS}
            group by h.id, m.id order by min(w.sequence)''', (first, last))
    lines = []
    for text, strongs, gloss, english, verses in rows:
        verse_list = ', '.join(sorted(set(verses.split(',')), key=int)) if verses else ''
        lines.append(f'- {text} ({strongs}, "{gloss}"): rendered {", ".join(sorted(set(english.split(","))))}; verses {verse_list}')
    return '\n'.join(lines)


def chapter_bounds(db, chapter):
    return db.execute('select min(w.sequence), max(w.sequence) from word w join verse v on v.id = w.verse_id where v.chapter_id = ?', (chapter,)).fetchone()


def passage_section(corpus, first, last, work):
    sections = [f'## Text\n\n{render_text(corpus, first, last)}',
                f'## Speeches\n\nEach line: verses, mode, speakers, then listeners.\n\n{render_speeches(corpus, first, last)}',
                f'## People and places\n\n{render_entities(corpus, first, last)}']
    if work == 1:
        original = render_original(corpus, first, last)
        if original:
            sections.append(f'## Original words\n\nHebrew or Greek headwords behind the English: meaning, the English words that render them, and the verses.\n\n{original}')
    return '\n\n'.join(sections)


def book_section(corpus, book, kinds):
    db = corpus.db
    headings = [text.strip() for _, number, text in verse_text(db, *chapter_bounds(db, corpus.book_chapters[book][0])) if number is None]
    lines = ["## The book's own heading\n\n" + '\n'.join(headings)] if headings else []
    lines.append('## Chapter notes\n\nNotes another session wrote for each chapter of this book, in the kinds this job covers.')
    names = {kind: corpus.kinds[kind][0] for kind in kinds}
    for chapter in corpus.book_chapters[book]:
        notes = dict(db.execute(
            f'select summary_kind_id, text from summary where chapter_id = ? and summary_kind_id in ({",".join("?" * len(kinds))})',
            (chapter, *kinds)))
        lines.append(f'### {corpus.chapter_label(chapter)}\n\n' + '\n'.join(f'{names[kind]}: {notes.get(kind, "(nothing of this kind)")}' for kind in kinds))
    return '\n\n'.join(lines)


def build_prompt(corpus, job):
    kind, id, _ = parse_job(job)
    label, _, _, kinds, (low, high) = job_target(corpus, job)
    if kind == 'book':
        missing = [chapter for chapter in corpus.book_chapters[id] if not is_applied(f'chapter-{chapter}')]
        if missing:
            raise SystemExit(f'{len(missing)} chapters of {label} have no notes yet, starting with {corpus.chapter_label(missing[0])}')
        length = f'Each note is one paragraph of {low} to {high} words.'
        target = f'the book {label}. Read its chapter notes below and write about the book as a whole: its arc, its main teachings, and what ties its chapters together, not a tour of its chapters.'
        body = book_section(corpus, id, kinds)
    else:
        length = f'Each note is a few sentences, {low} to {high} words.'
        if kind == 'chapter':
            first, last = chapter_bounds(corpus.db, id)
            target = f'{label}, a chapter.'
            work = corpus.books[corpus.chapters[id][0]]['work']
        else:
            _, first, last = corpus.range_row(id)
            target = f'the passage {label}, {corpus.range_span(id)}. Write only about the verses inside it. The text shows whole verses, so a passage that starts or ends mid-verse shows a little more.'
            work = corpus.range_work(id)
        body = passage_section(corpus, first, last, work)
    kind_lines = '\n'.join(f'- {corpus.kinds[kind][0]}: {corpus.kinds[kind][1]}' for kind in kinds)
    example = json.dumps({label: {corpus.kinds[kind][0]: '...' for kind in kinds}}, ensure_ascii=False, indent=2)
    head = INSTRUCTIONS.format(length=length, dash=EM_DASH, kinds=kind_lines, answer=JOBS / job / 'answer.json', example=example)
    return f'{head}\n## This job\n\nTarget: {target}\n\n{body}\n'


def inside_test(corpus, column, id):
    """Whether a cited (book, chapter, verse) falls inside the target."""
    if column == 'chapter_id':
        book, number = corpus.chapters[id]
        return lambda b, c, v: (b, c) == (book, number)
    if column == 'book_id':
        return lambda b, c, v: b == id
    _, first, last = corpus.range_row(id)
    order = lambda b, c, v: (corpus.positions[b], c, v or 0)
    start, end = order(*corpus.place(first)), order(*corpus.place(last))
    return lambda b, c, v: start <= order(b, c, v) <= end


def check_text(corpus, text, label, kind_name, limits, inside):
    problems = []
    count = len(text.split())
    if not limits[0] <= count <= limits[1]:
        problems.append(f'{count} words, but notes here run {limits[0]} to {limits[1]}')
    if '\n' in text or LIST_MARKER.search(text) or '**' in text:
        problems.append('write prose: no line breaks, lists, or markdown')
    if not SENTENCE_END.search(text):
        problems.append('end with a full sentence')
    checked = re.sub('Joseph Smith[–-]', 'Joseph Smith' + EM_DASH, text)
    covered, cited_inside = [], False
    for match in corpus.reference.finditer(checked):
        covered.append(match.span())
        book = corpus.book_ids[ALIASES.get(match.group(1), match.group(1))]
        chapter, verse = int(match.group(2)), int(match.group(3))
        end = int(match.group(4)) if match.group(4) else verse
        if (book, chapter, verse) not in corpus.verses or (book, chapter, end) not in corpus.verses:
            problems.append(f'{match.group(0)} names a verse that does not exist')
        cited_inside |= inside(book, chapter, verse)
    for match in corpus.unknown_reference.finditer(checked):
        if not any(start <= match.start(2) < end for start, end in covered):
            problems.append(f'"{match.group(0)}": write the book\'s full name, such as 1 Nephi 3:7 or D&C 89:3')
    if kind_name == 'Key verses' and not cited_inside:
        problems.append(f'cite at least one verse inside {label}, written in full like 1 Nephi 3:7')
    return [f'{label}, {kind_name}: {problem}' for problem in problems]


def apply(corpus, job):
    path = JOBS / job / 'answer.json'
    if not path.exists():
        raise SystemExit(f'no answer at {path}')
    try:
        answer = json.loads(path.read_text(encoding='utf-8'))
    except json.JSONDecodeError as error:
        raise SystemExit(f'the answer is not valid JSON: {error}')
    if not isinstance(answer, dict):
        raise SystemExit('the answer must be one JSON object keyed by target')
    label, column, id, kinds, limits = job_target(corpus, job)
    problems = [f'"{extra}" is not this job\'s target. The target is "{label}"' for extra in set(answer) - {label}]
    notes = answer.get(label)
    rows = []
    if not isinstance(notes, dict):
        problems.append(f'missing target "{label}"')
    else:
        names = {corpus.kinds[kind][0]: kind for kind in kinds}
        problems += [f'{label}: "{extra}" is not a kind here. Kinds: {", ".join(names)}' for extra in set(notes) - set(names)]
        inside = inside_test(corpus, column, id)
        for name, kind in names.items():
            text = notes.get(name)
            if not isinstance(text, str):
                problems.append(f'{label}: give {name} a string, empty when the target holds nothing of this kind')
                continue
            text = text.strip()
            if not text and name == 'Key verses':
                problems.append(f'{label}: Key verses is never empty')
            elif text:
                problems += check_text(corpus, text, label, name, limits, inside)
            rows.append((kind, text))
    if problems:
        print('Fix these in the answer and apply again:')
        print('\n'.join(f'- {problem}' for problem in problems))
        raise SystemExit(1)
    db = corpus.db
    db.execute('begin immediate')
    for kind, text in rows:
        if text:
            db.execute(
                f'''insert into summary (summary_kind_id, {column}, text) values (?, ?, ?)
                    on conflict (summary_kind_id, {column}) where {column} is not null do update set text = excluded.text''',
                (kind, id, text))
        else:
            db.execute(f'delete from summary where summary_kind_id = ? and {column} = ?', (kind, id))
    db.execute('commit')
    (JOBS / job / 'applied').touch()
    written = sum(1 for _, text in rows if text)
    print(f'applied {written} notes, {len(rows) - written} kinds left empty')


def main():
    command = sys.argv[1] if len(sys.argv) > 1 else ''
    corpus = Corpus(connect())
    if command == 'plan':
        print(json.dumps(plan(corpus)))
    elif command == 'prompt' and len(sys.argv) == 3:
        job = sys.argv[2]
        text = build_prompt(corpus, job)
        folder = JOBS / job
        folder.mkdir(parents=True, exist_ok=True)
        (folder / 'prompt.md').write_text(text, encoding='utf-8')
        print(f'prompt: {folder / "prompt.md"}\nanswer: {folder / "answer.json"}')
    elif command == 'apply' and len(sys.argv) == 3:
        apply(corpus, sys.argv[2])
    elif command == 'status':
        for name, jobs in zip(('chapters', 'ranges', 'books'), all_jobs(corpus)):
            print(f'{name}: {sum(map(is_applied, jobs))} of {len(jobs)} applied')
        print(f'notes: {corpus.db.execute("select count(*) from summary").fetchone()[0]}')
    else:
        raise SystemExit('usage: notes.py plan | prompt <job> | apply <job> | status')


if __name__ == '__main__':
    main()
