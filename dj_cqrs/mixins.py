from __future__ import unicode_literals

from itertools import chain

import six
from django.db import transaction
from django.db.models import Manager, Model, base

from dj_cqrs.constants import ALL_BASIC_FIELDS
from dj_cqrs.signals import MasterSignals, post_bulk_create, post_update
from dj_cqrs.factories import ReplicaFactory


def _check_cqrs_id(model_cls, model_name, mixin_name):
    """ Check that CQRS Model has CQRS_ID set up. """
    if model_name != mixin_name:
        assert model_cls.CQRS_ID, 'CQRS_ID must be set for every model, that uses CQRS.'


def _check_cqrs_fields(model_cls):
    """ Check that CQRS Master Model has correct CQRS fields configuration. """
    if model_cls.CQRS_FIELDS != ALL_BASIC_FIELDS:
        cqrs_field_names = set(model_cls.CQRS_FIELDS)
        opts = model_cls._meta
        model_name = model_cls.__name__

        pk_name = opts.pk.name
        assert pk_name in cqrs_field_names, \
            'PK is not in CQRS_FIELDS for model {}.'.format(model_name)

        field_names = {f.name for f in chain(opts.concrete_fields, opts.private_fields)}
        assert not cqrs_field_names - field_names, \
            'CQRS_FIELDS are not setup correctly for model {}.'.format(model_name)


class _MasterMeta(base.ModelBase):
    def __new__(mcs, *args):
        model_cls = super(_MasterMeta, mcs).__new__(mcs, *args)
        _check_cqrs_id(model_cls, args[0], 'MasterMixin')
        _check_cqrs_fields(model_cls)

        MasterSignals.register_model(model_cls)
        return model_cls


class _MasterManager(Manager):
    def update(self, queryset, **kwargs):
        """ Custom update method to support sending update signals.

        :param django.db.models.QuerySet queryset: Django Queryset (f.e. filter)
        :param kwargs: Update kwargs
        """
        with transaction.atomic():
            queryset.update(**kwargs)
        queryset.model.call_post_update(list(queryset.all()))


class MasterMixin(six.with_metaclass(_MasterMeta, Model)):
    """
    Mixin for the master CQRS model, that will send data updates to it's replicas.

    CQRS_FIELDS - Fields, that need to by synchronized between microservices.
    CQRS_ID - Unique CQRS identifier for all microservices.
    cqrs - Manager, that adds needed CQRS queryset methods.
    """
    CQRS_FIELDS = ALL_BASIC_FIELDS
    CQRS_ID = None

    objects = Manager()
    cqrs = _MasterManager()

    class Meta:
        abstract = True

    def model_to_cqrs_dict(self):
        """ CQRS serialization for transport payload. """
        opts = self._meta

        if isinstance(self.CQRS_FIELDS, six.string_types) and self.CQRS_FIELDS == ALL_BASIC_FIELDS:
            included_fields = None
        else:
            included_fields = self.CQRS_FIELDS

        data = {}
        for f in chain(opts.concrete_fields, opts.private_fields):
            if included_fields and (f.name not in included_fields):
                continue

            data[f.name] = f.value_from_object(self)
        return data

    def cqrs_sync(self):
        """ Manual instance synchronization. """
        if self._state.adding:
            return False

        try:
            self.refresh_from_db()
        except self._meta.model.DoesNotExist:
            return False

        MasterSignals.post_save(self._meta.model, instance=self)
        return True

    @classmethod
    def call_post_bulk_create(cls, instances):
        """ Post bulk create signal caller (django doesn't support it by default).

        .. code-block:: python
            # On PostgreSQL
            instances = model.objects.bulk_create(instances)
            model.call_post_bulk_create(instances)
        """
        post_bulk_create.send(cls, instances=instances)

    @classmethod
    def call_post_update(cls, instances):
        """ Post bulk update signal caller (django doesn't support it by default).

        .. code-block:: python
            # Used automatically by cqrs.update()
            qs = model.objects.filter(k1=v1)
            model.cqrs.update(qs, k2=v2)
        """
        post_update.send(cls, instances=instances)


class _ReplicaMeta(base.ModelBase):
    def __new__(mcs, *args):
        model_cls = super(_ReplicaMeta, mcs).__new__(mcs, *args)
        _check_cqrs_id(model_cls, args[0], 'ReplicaMixin')

        ReplicaFactory.register_model(model_cls)
        return model_cls


class ReplicaMixin(six.with_metaclass(_ReplicaMeta, Model)):
    """
    Mixin for the replica CQRS model, that will receive data updates from master.

    CQRS_ID - Unique CQRS identifier for all microservices.
    """
    CQRS_ID = None

    class Meta:
        abstract = True
