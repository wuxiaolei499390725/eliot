"""
Support for actions and tasks.

Actions have a beginning and an eventual end, and can be nested. Tasks are
top-level actions.
"""

from __future__ import unicode_literals, absolute_import

import threading
from uuid import uuid4
from itertools import count
from contextlib import contextmanager

from six import text_type as unicode

from ._message import Message
from ._util import safeunicode


class _ExecutionContext(threading.local):
    """
    Call stack-based context, storing the current L{Action}.

    Bit like L{twisted.python.context}, but:

    - Single purpose.
    - Allows support for Python context managers (this could easily be added
      to Twisted, though).
    - Does not require Twisted; Eliot should not require Twisted if possible.
    """
    def __init__(self):
        self._stack = []


    def push(self, action):
        """
        Push the given L{Action} to the front of the stack.

        @param action: L{Action} that will be used for log messages and as
            parent of newly created L{Action} instances.
        """
        self._stack.append(action)


    def pop(self):
        """
        Pop the front L{Action} on the stack.
        """
        self._stack.pop(-1)


    def current(self):
        """
        @return: The current front L{Action}, or C{None} if there is no
            L{Action} set.
        """
        if not self._stack:
            return None
        return self._stack[-1]


_context = _ExecutionContext()
currentAction = _context.current



class Action(object):
    """
    Part of a nested heirarchy of ongoing actions.

    An action has a start and an end; a message is logged for each.

    Actions should only be used from a single thread, by implication the
    thread where they were created.

    @ivar _numberOfMessages: The number of messages created in the context of
        this action.

    @ivar _identification: Fields identifying this action.

    @ivar _successFields: Fields to be included in successful finish message.

    @ivar _finished: L{True} if the L{Action} has finished, otherwise L{False}.
    """
    def __init__(self, logger, task_uuid, task_level, action_type,
                 serializers=None):
        """
        Initialize the L{Action} and log the start message.

        You probably do not want to use this API directly: use L{startAction}
        or L{startTask} instead.

        @param logger: The L{eliot.ILogger} to which to write
            messages.

        @param task_uuid: The uuid of the top-level task, e.g. C{"123525"}.

        @param task_level: The action's level in the task, e.g. C{"/"} or
            C{"/3/2/"}.

        @param action_type: The type of the action,
            e.g. C{"yourapp:subsystem:dosomething"}.

        @param serializers: Either a L{eliot._validation._ActionSerializers}
            instance or C{None}. In the latter case no validation or
            serialization will be done for messages generated by the
            L{Action}.
        """
        self._numberOfMessages = iter(count())
        self._successFields = {}
        self._logger = logger
        self._task_level = task_level
        self._identification = {"task_uuid": task_uuid,
                                "action_type": action_type,
                                }
        self._serializers = serializers
        self._finished = False

    def serialize_task_id(self):
        return "{}@{}~".format(self._identification["task_uuid"],
                               self._nextTaskLevel())

    @classmethod
    def continue_task(cls, logger, task_id):
        uuid, task_level = task_id.split("@")
        action = cls(logger, uuid, task_level, "remote_task")
        action._start({})
        return action

    def _nextTaskLevel(self):
        """
        Return the next C{task_level} for messages within this action.

        Called whenever a message is logged within the context of an action.

        @return: The message's C{task_level}.
        """
        return self._task_level + unicode(next(self._numberOfMessages) + 1)


    def _start(self, fields):
        """
        Log the start message.

        The action identification fields, and any additional given fields,
        will be logged.

        In general you shouldn't call this yourself, instead using a C{with}
        block or L{Action.finish}.
        """
        fields["action_status"] = "started"
        fields.update(self._identification)
        if self._serializers is None:
            serializer = None
        else:
            serializer = self._serializers.start
        Message(fields, serializer).write(self._logger, self)


    def finish(self, exception=None):
        """
        Log the finish message.

        The action identification fields, and any additional given fields,
        will be logged.

        In general you shouldn't call this yourself, instead using a C{with}
        block or L{Action.finish}.

        @param exception: C{None}, in which case the fields added with
            L{Action.addSuccessFields} are used. Or an L{Exception}, in
            which case an C{"exception"} field is added with the given
            L{Exception} type and C{"reason"} with its contents.
        """
        if self._finished:
            return
        self._finished = True
        serializer = None
        if exception is None:
            fields = self._successFields
            fields["action_status"] = "succeeded"
            if self._serializers is not None:
                serializer = self._serializers.success
        else:
            fields = {}
            fields["exception"] = "%s.%s" % (exception.__class__.__module__,
                                             exception.__class__.__name__)
            fields["reason"] = safeunicode(exception)
            fields["action_status"] = "failed"
            if self._serializers is not None:
                serializer = self._serializers.failure

        fields.update(self._identification)
        Message(fields, serializer).write(self._logger, self)


    def child(self, logger, action_type, serializers=None):
        """
        Create a child L{Action}.

        Rather than calling this directly, you can use L{startAction} to
        create child L{Action} using the execution context.

        @param logger: The L{eliot.ILogger} to which to write
            messages.

        @param action_type: The type of this action,
            e.g. C{"yourapp:subsystem:dosomething"}.

        @param serializers: Either a L{eliot._validation._ActionSerializers}
            instance or C{None}. In the latter case no validation or
            serialization will be done for messages generated by the
            L{Action}.
        """
        newLevel = self._nextTaskLevel() + "/"
        return self.__class__(logger,
                              self._identification["task_uuid"],
                              newLevel,
                              action_type,
                              serializers)


    def run(self, f, *args, **kwargs):
        """
        Run the given function with this L{Action} as its execution context.
        """
        _context.push(self)
        try:
            return f(*args, **kwargs)
        finally:
            _context.pop()


    def addSuccessFields(self, **fields):
        """
        Add fields to be included in the result message when the action
        finishes successfully.

        @param fields: Additional fields to add to the result message.
        """
        self._successFields.update(fields)


    # PEP 8 variant:
    add_success_fields = addSuccessFields


    @contextmanager
    def context(self):
        """
        Create a context manager that ensures code runs within action's context.

        The action does NOT finish when the context is exited.
        """
        _context.push(self)
        try:
            yield
        finally:
            _context.pop()


    # Python context manager implementation:
    def __enter__(self):
        """
        Push this action onto the execution context.
        """
        _context.push(self)
        return self


    def __exit__(self, type, exception, traceback):
        """
        Pop this action off the execution context, log finish message.
        """
        _context.pop()
        self.finish(exception)



def startAction(logger, action_type, _serializers=None, **fields):
    """
    Create a child L{Action}, figuring out the parent L{Action} from execution
    context, and log the start message.

    You can use the result as a Python context manager, or use the
    L{Action.finish} API to explicitly finish it.

         with startAction(logger, "yourapp:subsystem:dosomething",
                          entry=x) as action:
              do(x)
              result = something(x * 2)
              action.addSuccessFields(result=result)

    Or alternatively:

         action = startAction(logger, "yourapp:subsystem:dosomething",
                              entry=x)
         with action.context():
              do(x)
              result = something(x * 2)
              action.addSuccessFields(result=result)
         action.finish()

    @param logger: The L{eliot.ILogger} to which to write messages.

    @param action_type: The type of this action,
        e.g. C{"yourapp:subsystem:dosomething"}.

    @param _serializers: Either a L{eliot._validation._ActionSerializers}
        instance or C{None}. In the latter case no validation or serialization
        will be done for messages generated by the L{Action}.

    @param fields: Additional fields to add to the start message.

    @return: A new L{Action}.
    """
    parent = currentAction()
    if parent is None:
        return startTask(logger, action_type, _serializers, **fields)
    else:
        action = parent.child(logger, action_type, _serializers)
        action._start(fields)
        return action



def startTask(logger, action_type, _serializers=None, **fields):
    """
    Like L{action}, but creates a new top-level L{Action} with no parent.

    @param logger: The L{eliot.ILogger} to which to write messages.

    @param action_type: The type of this action,
        e.g. C{"yourapp:subsystem:dosomething"}.

    @param _serializers: Either a L{eliot._validation._ActionSerializers}
        instance or C{None}. In the latter case no validation or serialization
        will be done for messages generated by the L{Action}.

    @param fields: Additional fields to add to the start message.

    @return: A new L{Action}.
    """
    action = Action(logger, unicode(uuid4()), "/", action_type, _serializers)
    action._start(fields)
    return action
