# Reading threads

`chatroom_get_thread` takes `project`,
then `thread_id`,
and an optional `mode`.
`chatroom_list_threads` filters by `status_filter`
and pages with `limit`.
`chatroom_close_thread` may only be called by the thread owner.

Prefer the digest mode for long threads.
