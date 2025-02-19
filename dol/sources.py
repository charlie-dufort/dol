"""
This module contains key-value views of disparate sources.
"""
from typing import Iterator, Mapping, Iterable, Callable, Union, Any
from operator import itemgetter
from itertools import groupby as itertools_groupby

from dol.base import KvReader, KvPersister
from dol.trans import cached_keys
from dol.caching import mk_cached_store
from dol.util import copy_attrs
from dol.signatures import Sig


# ignore_if_module_not_found = suppress(ModuleNotFoundError)
#
# with ignore_if_module_not_found:
#     # To install: pip install mongodol
#     from mongodol.stores import (
#         MongoStore,
#         MongoTupleKeyStore,
#         MongoAnyKeyStore,
#     )


def identity_func(x):
    return x


def inclusive_subdict(d, include):
    return {k: d[k] for k in d.keys() & include}


def exclusive_subdict(d, exclude):
    return {k: d[k] for k in d.keys() - exclude}


class NotUnique(ValueError):
    """Raised when an iterator was expected to have only one element, but had more"""


NoMoreElements = type('NoMoreElements', (object,), {})()


def unique_element(iterator):
    element = next(iterator)
    if next(iterator, NoMoreElements) is not NoMoreElements:
        raise NotUnique('iterator had more than one element')
    return element


KvSpec = Union[Callable, Iterable[Union[str, int]], str, int]


def _kv_spec_to_func(kv_spec: KvSpec) -> Callable:
    if isinstance(kv_spec, (str, int)):
        return itemgetter(kv_spec)
    elif isinstance(kv_spec, Iterable):
        return itemgetter(*kv_spec)
    elif kv_spec is None:
        return identity_func
    return kv_spec


# TODO: This doesn't work
# KvSpec.from = _kv_spec_to_func  # I'd like to be able to couple KvSpec and it's
# conversion function (even more: __call__ instead of from)


# TODO: Generalize to several layers
#   Need a general tool for flattening views.
#   What we're doing here is giving access to a nested/tree structure through a key-value
#   view where keys specify tree paths.
#   Should handle situations where number layers are not fixed in advanced,
#   but determined by some rules executed dynamically.
#   Related DirStore and kv_walk.
class FlatReader(KvReader):
    """Get a 'flat view' of a store of stores.
    That is, where keys are `(first_level_key, second_level_key)` pairs.

    >>> readers = {
    ...     'fr': {1: 'un', 2: 'deux'},
    ...     'it': {1: 'uno', 2: 'due', 3: 'tre'},
    ... }
    >>> s = FlatReader(readers)
    >>> list(s)
    [('fr', 1), ('fr', 2), ('it', 1), ('it', 2), ('it', 3)]
    >>> s[('fr', 1)]
    'un'
    >>> s['it', 2]
    'due'
    """

    def __init__(self, readers):
        self._readers = readers

    def __iter__(self):
        # go through the first level paths:
        for first_level_path, reader in self._readers.items():
            for second_level_path in reader:  # go through the keys of the reader
                yield first_level_path, second_level_path

    def __getitem__(self, k):
        first_level_path, second_level_path = k
        return self._readers[first_level_path][second_level_path]


from collections import ChainMap


class FanoutReader(KvReader):
    """Get a 'fanout view' of a store of stores.
    That is, when a key is requested, the key is passed to all the stores, and results
    accumulated in a dict that is then returned.
    """

    def __init__(
        self,
        stores: Mapping,
        default=None,
        # *,
        keys: Iterable = None,
        # assert_all_have_key: bool=False,
    ):
        self._stores = stores
        # TODO: More control on what to do with missing keys
        self._default = default
        # self._assert_all_have_key = assert_all_have_key
        # TODO: Include more control over iteration mechanism. could be:
        #   - iter over all keys of all stores (using ChainMap)
        #   - iter over a specific store or stores
        #   - iter over a given iterable of keys
        if keys is None:
            keys = ChainMap(*self._stores.values())
        self._keys = keys

    def __getitem__(self, k):
        return {
            store_key: store.get(k, self._default)
            for store_key, store in self._stores.items()
        }

    def __iter__(self) -> Iterator:
        return iter(self._keys)

    def __len__(self) -> int:
        return len(self._keys)

    def __contains__(self, k) -> int:
        return k in self._keys


class SequenceKvReader(KvReader):
    """
    A KvReader that sources itself in an iterable of elements from which keys and values
    will be extracted and grouped by key.

    >>> docs = [{'_id': 0, 's': 'a', 'n': 1},
    ...  {'_id': 1, 's': 'b', 'n': 2},
    ...  {'_id': 2, 's': 'b', 'n': 3}]
    >>>

    Out of the box, SequenceKvReader gives you enumerated integer indices as keys,
    and the sequence items as is, as vals

    >>> s = SequenceKvReader(docs)
    >>> list(s)
    [0, 1, 2]
    >>> s[1]
    {'_id': 1, 's': 'b', 'n': 2}
    >>> assert s.get('not_a_key') is None

    You can make it more interesting by specifying a val function to compute the vals
    from the sequence elements

    >>> s = SequenceKvReader(docs, val=lambda x: (x['_id'] + x['n']) * x['s'])
    >>> assert list(s) == [0, 1, 2]  # as before
    >>> list(s.values())
    ['a', 'bbb', 'bbbbb']

    But where it becomes more useful is when you specify a key as well.
    SequenceKvReader will then compute the keys with that function, group them,
    and return as the value, the list of sequence elements that match that key.

    >>> s = SequenceKvReader(docs,
    ...         key=lambda x: x['s'],
    ...         val=lambda x: {k: x[k] for k in x.keys() - {'s'}})
    >>> assert list(s) == ['a', 'b']
    >>> assert s['a'] == [{'_id': 0, 'n': 1}]
    >>> assert s['b'] == [{'_id': 1, 'n': 2}, {'_id': 2, 'n': 3}]

    The cannonical form of key and val is a function, but if you specify a str, int,
    or iterable thereof,
    SequenceKvReader will make an itemgetter function from it, for your convenience.

    >>> s = SequenceKvReader(docs, key='_id')
    >>> assert list(s) == [0, 1, 2]
    >>> assert s[1] == [{'_id': 1, 's': 'b', 'n': 2}]

    The ``val_postproc`` argument is ``list`` by default, but what if we don't specify
    any?
    Well then you'll get an unconsumed iterable of matches

    >>> s = SequenceKvReader(docs, key='_id', val_postproc=None)
    >>> assert isinstance(s[1], Iterable)

    The ``val_postproc`` argument specifies what to apply to this iterable of matches.
    For example, you can specify ``val_postproc=next`` to simply get the first matched
    element:


    >>> s = SequenceKvReader(docs, key='_id', val_postproc=next)
    >>> assert list(s) == [0, 1, 2]
    >>> assert s[1] == {'_id': 1, 's': 'b', 'n': 2}

    We got the whole dict there. What if we just want we didn't want the _id, which is
    used by the key, in our val?

    >>> from functools import partial
    >>> all_but_s = partial(exclusive_subdict, exclude=['s'])
    >>> s = SequenceKvReader(docs, key='_id', val=all_but_s, val_postproc=next)
    >>> assert list(s) == [0, 1, 2]
    >>> assert s[1] == {'_id': 1, 'n': 2}

    Suppose we want to have the pair of ('_id', 'n') values as a key, and only 's'
    as a value...

    >>> s = SequenceKvReader(docs, key=('_id', 'n'), val='s', val_postproc=next)
    >>> assert list(s) == [(0, 1), (1, 2), (2, 3)]
    >>> assert s[1, 2] == 'b'

    But remember that using ``val_postproc=next`` will only give you the first match
    as a val.

    >>> s = SequenceKvReader(docs, key='s', val=all_but_s, val_postproc=next)
    >>> assert list(s) == ['a', 'b']
    >>> assert s['a'] == {'_id': 0, 'n': 1}
    >>> assert s['b'] == {'_id': 1, 'n': 2}   # note that only the first match is returned.

    If you do want to only grab the first match, but want to additionally assert
    that there is no more than one,
    you can specify this with ``val_postproc=unique_element``:

    >>> s = SequenceKvReader(docs, key='s', val=all_but_s, val_postproc=unique_element)
    >>> assert s['a'] == {'_id': 0, 'n': 1}
    >>> # The following should raise an exception since there's more than one match
    >>> s['b']  # doctest: +SKIP
    Traceback (most recent call last):
      ...
    sources.NotUnique: iterator had more than one element

    """

    def __init__(
        self,
        sequence: Iterable,
        key: KvSpec = None,
        val: KvSpec = None,
        val_postproc=list,
    ):
        """Make a SequenceKvReader instance,

        :param sequence: The iterable to source the keys and values from.
        :param key: Specification of how to extract a key from an iterable element.
            If None, will use integer keys from key, val = enumerate(iterable).
            key can be a callable, a str or int, or an iterable of strs and ints.
        :param val: Specification of how to extract a value from an iterable element.
            If None, will use the element as is, as the value.
            val can be a callable, a str or int, or an iterable of strs and ints.
        :param val_postproc: Function to apply to the iterable of vals.
            Default is ``list``, which will have the effect of values being lists of all
            vals matching a key.
            Another popular choice is ``next`` which will have the effect of values
            being the first matched to the key
        """
        self.sequence = sequence
        if key is not None:
            self.key = _kv_spec_to_func(key)
        else:
            self.key = None
        self.val = _kv_spec_to_func(val)
        self.val_postproc = val_postproc or identity_func
        assert isinstance(self.val_postproc, Callable)

    def kv_items(self):
        if self.key is not None:
            for k, v in itertools_groupby(self.sequence, key=self.key):
                yield k, self.val_postproc(map(self.val, v))
        else:
            for i, v in enumerate(self.sequence):
                yield i, self.val(v)

    def __getitem__(self, k):
        for kk, vv in self.kv_items():
            if kk == k:
                return vv
        raise KeyError(f'Key not found: {k}')

    def __iter__(self):
        yield from map(itemgetter(0), self.kv_items())


@cached_keys
class CachedKeysSequenceKvReader(SequenceKvReader):
    """SequenceKvReader but with keys cached. Use this one if you will perform multiple
    accesses to only some of the keys of the store"""


@mk_cached_store
class CachedSequenceKvReader(SequenceKvReader):
    """SequenceKvReader but with the whole mapping cached as a dict. Use this one if
    you will perform multiple accesses to the store"""


# TODO: Basically same could be acheived with
#  wrap_kvs(obj_of_data=methodcaller('__call__'))
class FuncReader(KvReader):
    """Reader that seeds itself from a data fetching function list
    Uses the function list names as the keys, and their returned value as the values.

    For example: You have a list of urls that contain the data you want to have access
    to.
    You can write functions that bare the names you want to give to each dataset,
    and have the function fetch the data from the url, extract the data from the
    response and possibly prepare it (we advise minimally, since you can always
    transform from the raw source, but the opposite can be impossible).

    >>> def foo():
    ...     return 'bar'
    >>> def pi():
    ...     return 3.14159
    >>> s = FuncReader([foo, pi])
    >>> list(s)
    ['foo', 'pi']
    >>> s['foo']
    'bar'
    >>> s['pi']
    3.14159

    You might want to give your own names to the functions.
    You might even have to (because the callable you're using doesn't have a `__name__`).
    In that case, you can specify a ``{name: func, ...}`` dict instead of a simple
    iterable.

    >>> s = FuncReader({'FU': foo, 'Pie': pi})
    >>> list(s)
    ['FU', 'Pie']
    >>> s['FU']
    'bar'

    """

    def __init__(self, funcs):
        # TODO: assert no free arguments (arguments are allowed but must all have
        #  defaults)
        if isinstance(funcs, Mapping):
            self.funcs = dict(funcs)
        else:
            self.funcs = {func.__name__: func for func in funcs}

    def __contains__(self, k):
        return k in self.funcs

    def __iter__(self):
        yield from self.funcs

    def __len__(self):
        return len(self.funcs)

    def __getitem__(self, k):
        return self.funcs[k]()  # call the func


class FuncDag(FuncReader):
    def __init__(self, funcs, **kwargs):
        super().__init__(funcs)
        self._sig = {fname: Sig(func) for fname, func in self._func.items()}
        # self._input_names = sum(self._sig)

    def __getitem__(self, k):
        return self._func_of_name[k]()  # call the func


import os

psep = os.path.sep

ddir = lambda o: [x for x in dir(o) if not x.startswith('_')]


def not_underscore_prefixed(x):
    return not x.startswith('_')


def _path_to_module_str(path, root_path):
    assert path.endswith('.py')
    path = path[:-3]
    if root_path.endswith(psep):
        root_path = root_path[:-1]
    root_path = os.path.dirname(root_path)
    len_root = len(root_path) + 1
    path_parts = path[len_root:].split(psep)
    if path_parts[-1] == '__init__.py':
        path_parts = path_parts[:-1]
    return '.'.join(path_parts)


class ObjReader(KvReader):
    def __init__(self, obj):
        self.src = obj
        copy_attrs(
            target=self,
            source=self.src,
            attrs=('__name__', '__qualname__', '__module__'),
            raise_error_if_an_attr_is_missing=False,
        )

    def __repr__(self):
        return f'{self.__class__.__qualname__}({self.src})'

    @property
    def _source(self):
        from warnings import warn

        warn('Deprecated: Use .src instead of ._source', DeprecationWarning, 2)
        return self.src


# class SourceReader(KvReader):
#     def __getitem__(self, k):
#         return getsource(k)

# class NestedObjReader(ObjReader):
#     def __init__(self, obj, src_to_key, key_filt=None, ):


# Pattern: Recursive navigation
# Note: Moved dev to independent package called "guide"
@cached_keys(keys_cache=set, name='Attrs')
class Attrs(ObjReader):
    """A simple recursive KvReader for the attributes of a python object.
    Keys are attr names, values are Attrs(attr_val) instances.

    Note: A more significant version of Attrs, along with many tools based on it,
    was moved to pypi package: guide.


        pip install guide
    """

    def __init__(self, obj, key_filt=not_underscore_prefixed, getattrs=dir):
        super().__init__(obj)
        self._key_filt = key_filt
        self.getattrs = getattrs

    @classmethod
    def module_from_path(
        cls, path, key_filt=not_underscore_prefixed, name=None, root_path=None
    ):
        import importlib.util

        if name is None:
            if root_path is not None:
                try:
                    name = _path_to_module_str(path, root_path)
                except Exception:
                    name = 'fake.module.name'
        spec = importlib.util.spec_from_file_location(name, path)
        foo = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(foo)
        return cls(foo, key_filt)

    def __iter__(self):
        yield from filter(self._key_filt, self.getattrs(self.src))

    def __getitem__(self, k):
        return self.__class__(getattr(self.src, k), self._key_filt, self.getattrs)

    def __repr__(self):
        return f'{self.__class__.__qualname__}({self.src}, {self._key_filt})'


Ddir = Attrs  # for back-compatibility, temporarily

import re


def _extract_first_identifier(string: str) -> str:
    m = re.match(r'\w+', string)
    if m:
        return m.group(0)
    else:
        return ''


def _dflt_object_namer(obj, dflt_name: str = 'name_not_found'):
    return (
        getattr(obj, '__name__', None)
        or _extract_first_identifier(getattr(obj, '__doc__'))
        or dflt_name
    )


class AttrContainer:
    """Convenience class to hold Key-Val pairs as attribute-val pairs, with all the
    magic methods of mappings.

    On the other hand, you will not get the usuall non-dunders (non magic methods) of
    ``Mappings``. This is so that you can use tab completion to access only the keys
    the container has, and not any of the non-dunder methods like ``get``, ``items``,
    etc.

    >>> da = AttrContainer(foo='bar', life=42)
    >>> da.foo
    'bar'
    >>> da['life']
    42
    >>> da.true = 'love'
    >>> len(da)  # count the number of fields
    3
    >>> da['friends'] = 'forever'  # write as dict
    >>> da.friends  # read as attribute
    'forever'
    >>> list(da)  # list fields (i.e. keys i.e. attributes)
    ['foo', 'life', 'true', 'friends']
    >>> 'life' in da  # check containement
    True

    >>> del da['friends']  # delete as dict
    >>> del da.foo # delete as attribute
    >>> list(da)
    ['life', 'true']
    >>> da._source  # the hidden Mapping (here dict) that is wrapped
    {'life': 42, 'true': 'love'}

    If you don't specify a name for some objects, ``AttrContainer`` will use the
    ``__name__`` attribute of the objects:

    >>> d = AttrContainer(map, tuple, obj='objects')
    >>> list(d)
    ['map', 'tuple', 'obj']

    You can also specify a different way of auto naming the objects:

    >>> d = AttrContainer('an', 'example', _object_namer=lambda x: f"_{len(x)}")
    >>> {k: getattr(d, k) for k in d}
    {'_2': 'an', '_7': 'example'}

    .. seealso:: Objects in ``py2store.utils.attr_dict`` module
    """

    _source = None

    def __init__(
        self,
        *objects,
        _object_namer: Callable[[Any], str] = _dflt_object_namer,
        **named_objects,
    ):
        if objects:
            auto_named_objects = {_object_namer(obj): obj for obj in objects}
            self._validate_named_objects(auto_named_objects, named_objects)
            named_objects = dict(auto_named_objects, **named_objects)

        super().__setattr__('_source', {})
        for k, v in named_objects.items():
            setattr(self, k, v)

    @staticmethod
    def _validate_named_objects(auto_named_objects, named_objects):
        if not all(map(str.isidentifier, auto_named_objects)):
            raise ValueError(
                'All names produced by _object_namer should be valid python identifiers:'
                f" {', '.join(x for x in auto_named_objects if not x.isidentifier())}"
            )
        clashing_names = auto_named_objects.keys() & named_objects.keys()
        if clashing_names:
            raise ValueError(
                'Some auto named objects clashed with named ones: '
                f"{', '.join(clashing_names)}"
            )

    def __getitem__(self, k):
        return self._source[k]

    def __setitem__(self, k, v):
        setattr(self, k, v)

    def __delitem__(self, k):
        delattr(self, k)

    def __iter__(self):
        return iter(self._source.keys())

    def __len__(self):
        return len(self._source)

    def __setattr__(self, k, v):
        self._source[k] = v
        super().__setattr__(k, v)

    def __delattr__(self, k):
        del self._source[k]
        super().__delattr__(k)

    def __contains__(self, k):
        return k in self._source

    def __repr__(self):
        return super().__repr__()


# TODO: Make it work with a store, without having to load and store the values explicitly.
class AttrDict(AttrContainer, KvPersister):
    """Convenience class to hold Key-Val pairs with both a dict-like and struct-like
    interface.

    The dict-like interface has just the basic get/set/del/iter/len
    (all "dunders": none visible as methods). There is no get, update, etc.
    This is on purpose, so that the only visible attributes
    (those you get by tab-completion for instance) are the those you injected.

    >>> da = AttrDict(foo='bar', life=42)

    You get the "keys as attributes" that you get with ``AttrContainer``:

    >>> da.foo
    'bar'

    But additionally, you get the extra ``Mapping`` methods:

    >>> list(da.keys())
    ['foo', 'life']
    >>> list(da.values())
    ['bar', 42]
    >>> da.get('foo')
    'bar'
    >>> da.get('not_a_key', 'default')
    'default'

    You can assign through key or attribute assignment:

    >>> da['true'] = 'love'
    >>> da.friends = 'forever'
    >>> list(da.items())
    [('foo', 'bar'), ('life', 42), ('true', 'love'), ('friends', 'forever')]


    etc.

    .. seealso:: Objects in ``py2store.utils.attr_dict`` module
    """
