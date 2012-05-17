# -*- coding: utf-8 -*-
#
# Copyright © 2012 Red Hat, Inc.
#
# This software is licensed to you under the GNU General Public
# License as published by the Free Software Foundation; either version
# 2 of the License (GPLv2) or (at your option) any later version.
# There is NO WARRANTY for this software, express or implied,
# including the implied warranties of MERCHANTABILITY,
# NON-INFRINGEMENT, or FITNESS FOR A PARTICULAR PURPOSE. You should
# have received a copy of GPLv2 along with this software; if not, see
# http://www.gnu.org/licenses/old-licenses/gpl-2.0.txt.

from gettext import gettext as _
import time

import status

from pulp.gc_client.framework.extensions import PulpCliCommand

# -- sync commands ------------------------------------------------------------

class RunSyncCommand(PulpCliCommand):
    def __init__(self, context, name, description):
        PulpCliCommand.__init__(self, name, description, self.sync)
        self.context = context

        self.create_option('--repo-id', _('identifies the repository to sync'), required=True)

        # I originally wrote this when the flag was for foreground. In case we
        # move back to that model, I've left the developer the description already.
        # You're welcome.
        #
        # d = 'if specified, the progress for the sync will be continually displayed ' \
        #     'on screen and the CLI process will not end until it is completed; the ' \
        #    'progress can be viewed later using the status command if this is not specified'

        d = 'if specified, the CLI process will end but the sync will continue on ' \
            'the server; the progress can be later displayed using the status command'
        self.create_flag('--bg', _(d))

    def sync(self, **kwargs):
        repo_id = kwargs['repo-id']
        foreground = not kwargs['bg']

        self.context.prompt.render_title(_('Synchronizing Repository [%(r)s]') % {'r' : repo_id})

        # See if an existing sync is running for the repo. If it is, resume
        # progress tracking.
        existing_sync_tasks = self.context.server.tasks.get_repo_sync_tasks(repo_id).response_body
        if len(existing_sync_tasks) > 0:
            task_id = relevant_existing_task_id(existing_sync_tasks)

            msg = _('An existing sync has already been triggered for this repository. ')

            if foreground:
                msg += _('Its progress will be tracked below.')
            self.context.prompt.render_paragraph(msg)

        else:
            # Trigger the actual sync
            response = self.context.server.repo_actions.sync(repo_id, None)
            task_id = response.response_body.task_id

        if foreground:
            display_status(self.context, task_id)
        else:
            msg = 'The status of this sync can be displayed using the status command.'
            self.context.prompt.render_paragraph(_(msg))

class StatusCommand(PulpCliCommand):
    def __init__(self, context, name, description):
        PulpCliCommand.__init__(self, name, description, self.status)
        self.context = context

        self.create_option('--repo-id', 'identifies the repository', required=True)

    def status(self, **kwargs):
        repo_id = kwargs['repo-id']
        self.context.prompt.render_title('Repository Status [%s]' % repo_id)

        # This looks dumb but the task lookup doesn't know if there are no tasks
        # for a repo v. the repo doesn't exist. We call this to let the not found
        # exception bubble if it's not a valid repo.
        self.context.server.repo.repository(repo_id)

        # Load the existing sync tasks
        existing_sync_tasks = self.context.server.tasks.get_repo_sync_tasks(repo_id).response_body
        if len(existing_sync_tasks) > 0:
            task_id = relevant_existing_task_id(existing_sync_tasks)

            msg = 'A sync task is queued on the server. Its progress will be tracked below.'
            self.context.prompt.render_paragraph(_(msg))
            display_status(self.context, task_id)

        else:
            self.context.prompt.render_paragraph(_('There are no sync tasks currently queued in the server.'))

# -- utility ------------------------------------------------------------------

def relevant_existing_task_id(existing_sync_tasks):
    """
    Analyzes the list of existing sync tasks to determine which should be
    tracked by the CLI.

    @param existing_sync_tasks: list of task instances retrieved from the server
    @type  existing_sync_tasks: list

    @return: ID of the task that should be displayed
    @rtype:  str
    """

    # At this point, we have at least one sync task but that doesn't
    # mean it's running yet. It shouldn't, however, be completed as
    # it wouldn't come back in the lookup. That should leave two
    # possibilities: waiting or running.
    #
    # There will only be one running, so that case is easy: if we find
    # a running task start displaying it.
    #
    # If there are no running tasks, the waiting ones are ordered such
    # that the first one will execute next, so use that task ID and
    # start the display process (it will handle waiting accordingly.

    running_tasks = [t for t in existing_sync_tasks if t.is_running()]
    waiting_tasks = [t for t in existing_sync_tasks if t.is_waiting()]

    if len(running_tasks) > 0:
        task_id = running_tasks[0].task_id
    else:
        task_id = waiting_tasks[0].task_id

    return task_id

def display_status(context, task_id):

    response = context.server.tasks.get_task(task_id)
    renderer = status.StatusRenderer(context)

    m = 'This command may be exited by pressing ctrl+c without affecting the actual sync operation on the server.'
    context.prompt.render_paragraph(_(m))

    # Handle the cases where we don't want to honor the foreground request
    if response.response_body.is_rejected():
        announce = _('The request to synchronize repository was rejected')
        description = _('This is likely due to an impending delete request for the repository.')

        context.prompt.render_failure_message(announce)
        context.prompt.render_paragraph(description)
        return

    if response.response_body.is_postponed():
        a  = 'The request to synchronize the repository was accepted but postponed '\
             'due to one or more previous requests against the repository. The sync will '\
             'take place at the earliest possible time.'
        context.prompt.render_paragraph(_(a))
        return

    # If we're here, the sync should be running or hopefully about to run
    begin_spinner = context.prompt.create_spinner()
    poll_frequency_in_seconds = context.client_config.getfloat('output', 'poll_frequency_in_seconds')

    try:
        while not response.response_body.is_completed():
            if response.response_body.is_waiting():
                begin_spinner.next(_('Waiting to begin'))
            else:
                renderer.display_report(response.response_body.progress)

            time.sleep(poll_frequency_in_seconds)

            response = context.server.tasks.get_task(response.response_body.task_id)

    except KeyboardInterrupt:
        # If the user presses ctrl+c, don't let the error bubble up, just
        # exit gracefully
        return

    # Even after completion, we still want to display the report one last
    # time in case there was no poll between, say, the middle of the
    # package download and when the task itself reports as finished. We
    # don't want to leave the UI in that half-finished state so this final
    # call is to clean up and render the completed report.
    renderer.display_report(response.response_body.progress)

    if response.response_body.was_successful():
        context.prompt.render_success_message('Successfully synchronized repository')
    else:
        context.prompt.render_failure_message('Error during repository synchronization')
        context.prompt.render_failure_message(response.response_body.exception)
