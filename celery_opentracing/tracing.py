# Copyright (C) 2019 SignalFx, Inc. All rights reserved.
from copy import copy
import traceback
import logging

from celery.signals import (
    before_task_publish, after_task_publish,
    task_prerun, task_postrun, task_failure,
    task_retry
)
import opentracing.ext.tags as ext_tags
from six import text_type
import celery.app.base
import opentracing

log = logging.getLogger(__name__)

context_headers = '_celery_opentracing_context'
spans_attr = '_ct_spans'


class CeleryTracing(celery.app.base.Celery):

    def __init__(self, *args, **kwargs):
        self._tracer = kwargs.pop('tracer', opentracing.tracer)
        self._propagate = kwargs.pop('propagate', True)
        self._span_tags = kwargs.pop('span_tags', {})
        log.debug('Using %s as CeleryTracing tracer.', self._tracer)
        super(CeleryTracing, self).__init__(*args, **kwargs)

        self.connect_traced_handlers()

    def connect_traced_handlers(self):
        if self._propagate:
            before_task_publish.connect(self._prepublish, weak=False)
            after_task_publish.connect(self._postpublish, weak=False)
        task_prerun.connect(self._start_span, weak=False)
        task_failure.connect(self._tag_error, weak=False)
        task_retry.connect(self._tag_retry, weak=False)
        task_postrun.connect(self._finish_span, weak=False)
        log.debug('Registered CeleryTracing signal handlers.')

    def disconnect_traced_handlers(self):
        if self._propagate:
            before_task_publish.disconnect(self._prepublish)
            after_task_publish.disconnect(self._postpublish)
        task_prerun.disconnect(self._start_span)
        task_failure.disconnect(self._tag_error)
        task_retry.disconnect(self._tag_retry)
        task_postrun.disconnect(self._finish_span)
        log.debug('Disconnected CeleryTracing signal handlers.')

    def _is_local(self, item):
        """Signals are registered globally, so ensure task's app is this CeleryTracing instance"""
        app = None
        if hasattr(item, 'app'):
            app = item.app
        is_local = app is self
        return is_local

    def _prepublish(self, *args, **kwargs):
        task = self.tasks.get(kwargs.get('sender'))
        if not self._is_local(task):
            return

        scope = self._tracer.start_active_span('publish {0.name}'.format(task), tags=copy(self._span_tags))
        span = scope.span

        task_id = kwargs.get('headers', {}).get('id') or kwargs.get('body', {}).get('id')
        if task_id is None:
            raise RuntimeError('task_id is never expected to be None.')
        span.set_tag('celery.task.id', task_id)

        self._set_span_tags(span, task.request)

        body = kwargs.get('body')
        if isinstance(body, tuple):  # celery 4.0
            body = body[2]
        self._set_span_tags(span, body)

        headers = kwargs.get('headers')
        if headers is not None:
            self._set_span_tags(span, headers)
            if self._propagate:
                headers[context_headers] = {}
                self._tracer.inject(span.context, opentracing.Format.TEXT_MAP,
                                    headers[context_headers])

        if not hasattr(task, spans_attr):
            setattr(task, spans_attr, {})
        getattr(task, spans_attr)['publish:{}'.format(task_id)] = span

    def _postpublish(self, *args, **kwargs):
        task = self.tasks.get(kwargs.get('sender'))
        if not self._is_local(task):
            return

        task_id = kwargs.get('headers', {}).get('id') or kwargs.get('body', {}).get('id')
        span = self._get_span(task, 'publish:{}'.format(task_id), remove=True)
        active_scope = self._tracer.scope_manager.active
        if active_scope.span is span:
            active_scope.close()
        else:
            span.finish()
            log.warn('SCOPE LEAK! %s != %s', active_scope.span, span)

    def _start_span(self, *args, **kwargs):
        task = kwargs.get('task', kwargs.get('sender'))
        if not self._is_local(task):
            return
        parent = None
        if self._propagate:
            context = getattr(task.request, context_headers, None) or task.request.headers.get(context_headers)
            if context:
                parent = self._tracer.extract(opentracing.Format.TEXT_MAP, context)

        span = self._tracer.start_active_span(task.name, child_of=parent, ignore_active_span=True,
                                              tags=copy(self._span_tags)).span

        request = task.request
        task_id = kwargs.get('task_id', request.correlation_id)
        if task_id is None:
            raise RuntimeError('task_id is never expected to be None.')

        span.set_tag('celery.task.id', task_id)
        self._set_span_tags(span, task.request)

        if not hasattr(task, spans_attr):
            setattr(task, spans_attr, {})
        getattr(task, spans_attr)[task_id] = span

    def _tag_error(self, *args, **kwargs):
        task = kwargs.get('sender')
        if not self._is_local(task):
            return
        span = self._get_span(task, kwargs.get('task_id'))
        span.set_tag(ext_tags.ERROR, True)

        exc = kwargs.get('exception')
        einfo = kwargs.get('einfo')
        if exc is None and einfo is not None:
            exc = einfo.exception

        logged_error = {'event': ext_tags.ERROR}
        if exc is not None:
            logged_error.update({'message': str(exc),
                                 'error.object': exc,
                                 'error.kind': exc.__class__.__name__})

            tb = kwargs.get('traceback')
            if tb is None:
                if einfo is not None:
                    tb = einfo.traceback
            if tb is not None:
                logged_error['stack'] = text_type('').join(traceback.format_tb(tb))

        span.log_kv(logged_error)

    def _tag_retry(self, *args, **kwargs):
        task = kwargs.get('sender')
        if not self._is_local(task):
            return
        span = self._get_span(task, kwargs.get('request', {}).get('id'))
        span.set_tag('celery.retry', True)
        span.set_tag('celery.retry.reason', text_type(kwargs.get('reason')))

        einfo = kwargs.get('einfo')
        if einfo is not None:
            exc = einfo.exception
            logged_error = {'event': ext_tags.ERROR}
            logged_error.update({'message': str(exc),
                                 'error.object': exc,
                                 'error.kind': exc.__class__.__name__})

            tb = einfo.traceback
            if tb is not None:
                if isinstance(tb, list):
                    tb = text_type('').join(traceback.format_tb(tb))
                logged_error['stack'] = tb

            span.log_kv(logged_error)

    def _finish_span(self, *args, **kwargs):
        task = kwargs.get('sender')
        if not self._is_local(task):
            return

        span = self._get_span(task, kwargs.get('task_id'), remove=True)
        active_scope = self._tracer.scope_manager.active
        if active_scope.span is span:
            active_scope.close()
        else:
            span.finish()
            log.warn('SCOPE LEAK! %s != %s', active_scope.span, span)

    @staticmethod
    def _get_span(task, task_id, remove=False):
        if hasattr(task, spans_attr):
            spans = getattr(task, spans_attr)
            if remove:
                return spans.pop(task_id)
            return spans[task_id]

    @staticmethod
    def _set_span_tags(span, headers):
        span.set_tag(ext_tags.COMPONENT, 'celery')
        tags = ('countdown', 'delivery_info', 'eta', 'expires', 'group', 'hostname', 'origin', 'retries', 'timelimit')
        for tag in tags:
            val = headers.get(tag)
            if val not in (None, '', [None, None], (None, None)):
                if tag == 'delivery_info':
                    for subtag in ('exchange', 'priority', 'redelivered', 'routing_key', 'queue'):
                        subval = val.get(subtag)
                        if subval not in (None, ''):
                            span.set_tag('celery.delivery.{}'.format(subtag), subval)

                            # delivery_info not always populated so we overwrite OT tag
                            # with more detail when possible, with preference at queue level
                            if subtag in ('exchange', 'routing_key', 'queue'):
                                span.set_tag(ext_tags.MESSAGE_BUS_DESTINATION, subval)
                    continue

                if tag == 'hostname':
                    tag = 'worker.hostname'
                if tag == 'origin':
                    tag = 'task.origin'
                span.set_tag('celery.{}'.format(tag), val)