import datetime
import operator

from flask import request
from flask_peewee.utils import models_to_path
from peewee import *
from wtforms import fields, form, widgets
from wtfpeewee.fields import ModelSelectField, ModelSelectMultipleField


class BooleanSelectField(fields.SelectFieldBase):
    widget = widgets.Select()

    def iter_choices(self):
        yield ('1', 'True', self.data)
        yield ('', 'False', not self.data)

    def process_data(self, value):
        try:
            self.data = bool(value)
        except (ValueError, TypeError):
            self.data = None

    def process_formdata(self, valuelist):
        if valuelist:
            try:
                self.data = bool(valuelist[0])
            except ValueError:
                raise ValueError(self.gettext(u'Invalid Choice: could not coerce'))


LOOKUP_TYPES = {
    'eq': 'equal to',
    'lt': 'less than',
    'gt': 'greater than',
    'lte': 'less than or equal to',
    'gte': 'greater than or equal to',
    'ne': 'not equal to',
    'istartswith': 'starts with',
    'icontains': 'contains',
    'in': 'is one of',
    
    # special lookups that require some preprocessing
    'today': 'is today',
    'yesterday': 'is yesterday',
    'this_week': 'this week',
    'lte_days_ago': 'less than ... days ago',
    'gte_days_ago': 'greater than ... days ago',
}

FIELD_TYPES = {
    'foreign_key': [ForeignKeyField],
    'text': [CharField, TextField],
    'numeric': [PrimaryKeyField, IntegerField, FloatField, DecimalField],
    'boolean': [BooleanField],
    'datetime': [DateTimeField],
}

INV_FIELD_TYPES = dict((v,k) for k in FIELD_TYPES for v in FIELD_TYPES[k])

FIELDS_TO_LOOKUPS = {
    'foreign_key': ['eq', 'in'],
    'text': ['eq', 'icontains', 'istartswith'],
    'numeric': ['eq', 'ne', 'lt', 'lte', 'gt', 'gte', 'in'],
    'boolean': ['eq'],
    'datetime': ['today', 'yesterday', 'this_week', 'lte_days_ago', 'gte_days_ago'],
}

CONVERTERS = {
    (ForeignKeyField, 'eq'): lambda f: ModelSelectField(model=f.to),
    (ForeignKeyField, 'in'): lambda f: ModelSelectMultipleField(model=f.to),
    (PrimaryKeyField, 'eq'): lambda f: ModelSelectField(model=f.model),
    (PrimaryKeyField, 'in'): lambda f: ModelSelectMultipleField(model=f.model),
    (DateTimeField, 'today'): lambda f: fields.HiddenField(),
    (DateTimeField, 'yesterday'): lambda f: fields.HiddenField(),
    (DateTimeField, 'this_week'): lambda f: fields.HiddenField(),
    (BooleanField, 'eq'): lambda f: BooleanSelectField(),
}

def lookups_for_field(field):
    """
    Get a list of valid lookups for a given field type.  Lookups are expressed
    as a 2-tuple of (<lookup suffix>, <human description>)
    
    >>> lookups_for_field(User.username)
    [('eq', 'equal to'), ('icontains', 'contains'), ('istartswith', 'starts with')]
    """
    field_class = type(field)
    return [
        (lookup, LOOKUP_TYPES[lookup]) \
            for lookup in FIELDS_TO_LOOKUPS[INV_FIELD_TYPES[field_class]]
    ]

def get_lookups(model, exclude, prefix, models):
    """
    Retrieve a list of valid lookups for a model, taking into account any fields
    that need to be excluded.  Packaged up as a list of tuples containing the
    prefix used and the models in the join chain.
    """
    lookups = []
    for field_obj in model._meta.get_fields():
        if field_obj.name not in exclude:
            lookups.append((field_obj, prefix, models, lookups_for_field(field_obj)))
    
    return lookups

def form_field_for_lookup(field, lookup):
    """
    For a given field object and lookup, return the proper form field for
    inputting a value.  Mappings are provided in the CONVERTERS dictionary.
    """
    field_class = type(field)
    default = lambda f: fields.TextField()
    return CONVERTERS.get((field_class, lookup), default)(field)

def get_fields(model, exclude):
    """
    For a given model instance, retrieve a dictionary of all field/lookups for
    a model.  These fields are unbound and will be used to construct a filter
    form by the get_filter_form() function.
    
    >>> get_fields(Message)
    {
        'content__eq': <UnboundField(TextField, (), {})>,
        'content__icontains': <UnboundField(TextField, (), {})>,
        'content__istartswith': <UnboundField(TextField, (), {})>,
        'id__eq': <UnboundField(ModelSelectField, (), {'model': <class 'models.Message'>})>,
        ...
    }
    """
    fields = {}
    for field_name, field_obj in model._meta.fields.items():
        if field_name in exclude:
            continue
        
        for lookup, _ in lookups_for_field(field_obj):
            form_field = form_field_for_lookup(field_obj, lookup)
            if form_field:
                fields['%s__%s' % (field_name, lookup)] = form_field
    
    return fields

def get_filter_form(model, exclude, prefix=''):
    """
    Create a form instance suitable for use in the admin templates, optionally
    with a given prefix.  The prefix is used to denote related lookups.
    """
    frm = form.BaseForm(get_fields(model, exclude), prefix=prefix)
    frm.process(None)
    return frm

def _rd(n):
    return datetime.date.today() + datetime.timedelta(days=n)

class FilterPreprocessor(object):
    def process_lookup(self, raw_lookup, values=None):
        """
        Returns a list of dictionaries
        """
        if '__' in raw_lookup:
            field_part, lookup = raw_lookup.rsplit('__', 1)
            if hasattr(self, 'process_%s' % lookup):
                return getattr(self, 'process_%s' % lookup)(field_part, values)
        
        return [{raw_lookup: v} for v in values]
    
    def process_in(self, field_part, values):
        return [{'%s__in' % field_part: values}]
    
    def process_today(self, field_part, values):
        return [{
            '%s__gte' % field_part: _rd(0),
            '%s__lt' % field_part: _rd(1),
        }]
    
    def process_yesterday(self, field_part, values):
        return [{
            '%s__gte' % field_part: _rd(-1),
            '%s__lt' % field_part: _rd(0),
        }]
    
    def process_this_week(self, field_part, values):
        return [{
            '%s__gte' % field_part: _rd(-6),
            '%s__lt' % field_part: _rd(1),
        }]
    
    def process_lte_days_ago(self, field_part, values):
        return [{
            '%s__gte' % field_part: _rd(-1 * int(value)),
        } for value in values]
    
    def process_gte_days_ago(self, field_part, values):
        return [{
            '%s__lte' % field_part: _rd(-1 * int(value)),
        } for value in values]


class QueryFilter(object):
    def __init__(self, query, exclude_fields=None, ignore_filters=None, related=None):
        self.query = query
        self.model = self.query.model

        # fix any foreign key fields
        self.exclude_fields = []
        for field_name in exclude_fields or ():
            if field_name in self.model._meta.fields:
                self.exclude_fields.append(field_name)
            else:
                self.exclude_fields.append(self.model._meta.rel_fields[field_name])
        
        self.ignore_filters = ignore_filters or ()
        
        # a list of related QueryFilter() objects
        self.related = related or ()
    
    def get_form(self, prefix=''):
        return get_filter_form(self.model, self.exclude_fields, prefix=prefix)
    
    def get_forms(self, prefix=''):
        forms = [self.get_form(prefix)]
        for related_filter in self.related:
            path = models_to_path([self.model, related_filter.model])
            forms.extend(related_filter.get_forms(prefix + path + '__'))
        return forms
    
    def get_lookups(self, prefix='', models=None):
        models = models or []
        lookups = get_lookups(self.model, self.exclude_fields, prefix, models)
        for related_filter in self.related:
            rel_model = related_filter.model
            path = models_to_path([self.model, rel_model])
            lookups.extend(related_filter.get_lookups(prefix + path + '__', models + [rel_model]))
        return lookups
    
    def get_preprocessor(self):
        return FilterPreprocessor()
    
    def process_request(self):
        self.raw_lookups = []
        
        filters = []
        lookups_by_column = {}
        
        preprocessor = self.get_preprocessor()
        
        # preprocessing -- essentially a place to store "raw" filters (used to
        # reconstruct filter widgets on frontend), and a place to munge filters
        # that have special meaning, i.e. "today"
        for key in request.args:
            if key in self.ignore_filters:
                continue
            
            values = request.args.getlist(key)
            self.raw_lookups.append((key, values))
            
            # preprocessor returns a list of filters to apply
            filters.extend(preprocessor.process_lookup(key, values))
        
        # at this point, need to figure out which parts of the query to "AND"
        # together and which to "OR" together ... this is naive and simply groups
        # lookups on the same column together in an OR clause.
        for filter_dict in filters:
            for field_part, value in filter_dict.items():
                if '__' in field_part:
                    column, lookup = field_part.rsplit('__', 1)
                else:
                    column = field_part
            
            lookups_by_column.setdefault(column, [])
            lookups_by_column[column].append(Q(**filter_dict))
        
        return [reduce(operator.or_, lookups) for lookups in lookups_by_column.values()]
    
    def get_filtered_query(self):
        nodes = self.process_request()
        if nodes:
            return self.query.filter(*nodes)
        
        return self.query
