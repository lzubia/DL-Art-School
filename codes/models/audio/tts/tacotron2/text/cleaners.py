""" from https://github.com/keithito/tacotron """

'''
Cleaners are transformations that run over the input text at both training and eval time.

Cleaners can be selected by passing a comma-delimited list of cleaner names as the "cleaners"
hyperparameter. Some cleaners are English-specific. You'll typically want to use:
  1. "english_cleaners" for English text
  2. "transliteration_cleaners" for non-English text that can be transliterated to ASCII using
     the Unidecode library (https://pypi.python.org/pypi/Unidecode)
  3. "basic_cleaners" if you do not want to transliterate (in this case, you should also update
     the symbols in symbols.py to match your data).
'''


# Regular expression matching whitespace:
import re
from unidecode import unidecode
from .numbers import normalize_numbers
_whitespace_re = re.compile(r'\s+')

# List of (regular expression, replacement) pairs for abbreviations:
_abbreviations = [(re.compile('\\b%s\\.' % x[0], re.IGNORECASE), x[1]) for x in [
    ('mrs', 'misess'),
    ('mr', 'mister'),
    ('dr', 'doctor'),
    ('st', 'saint'),
    ('co', 'company'),
    ('jr', 'junior'),
    ('maj', 'major'),
    ('gen', 'general'),
    ('drs', 'doctors'),
    ('rev', 'reverend'),
    ('lt', 'lieutenant'),
    ('hon', 'honorable'),
    ('sgt', 'sergeant'),
    ('capt', 'captain'),
    ('esq', 'esquire'),
    ('ltd', 'limited'),
    ('col', 'colonel'),
    ('ft', 'fort'),
]]


def expand_abbreviations(text):
    for regex, replacement in _abbreviations:
        text = re.sub(regex, replacement, text)
    return text


def expand_numbers(text):
    return normalize_numbers(text)


def lowercase(text):
    return text.lower()


def collapse_whitespace(text):
    return re.sub(_whitespace_re, ' ', text)


def convert_to_ascii(text):
    return unidecode(text)


def basic_cleaners(text):
    '''Basic pipeline that lowercases and collapses whitespace without transliteration.'''
    text = lowercase(text)
    text = collapse_whitespace(text)
    return text


def transliteration_cleaners(text):
    '''Pipeline for non-English text that transliterates to ASCII.'''
    text = convert_to_ascii(text)
    text = lowercase(text)
    text = collapse_whitespace(text)
    return text


############## OUR MODIFICATION ###############

def expand_numbers_basque(text):
    number_map = {
        "0": "zero", "1": "bat", "2": "bi", "3": "hiru",
        "4": "lau", "5": "bost", "6": "sei", "7": "zazpi",
        "8": "zortzi", "9": "bederatzi", "10": "hamar"
    }
    for digit, word in number_map.items():
        text = text.replace(digit, word)
    return text


def expand_abbreviations_basque(text):
    abbreviations = {
        "etab.": "eta abar",  # and others
        "adb.": "adibidez",  # for example
        "geh.": "gehienez",  # at most
    }
    for abbr, full_form in abbreviations.items():
        text = text.replace(abbr, full_form)
    return text


def handle_special_letters(text):
    """Handles Basque-specific letters like 'ñ' and 'x'."""
    text = text.replace("x", "sh")
    text = text.replace("ñ", "n")
    text = text.replace("tx", "ch")
    return text


def english_cleaners(text):
    '''Pipeline for English text, including number and abbreviation expansion.'''
    text = text.lower()
    text = expand_numbers_basque(text)
    text = expand_abbreviations_basque(text)
    text = handle_special_letters(text)
    text = collapse_whitespace(text)
    text = text.replace('"', '')
    return text
