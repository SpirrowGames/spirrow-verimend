# Migration notes

The MCP server used to listen on port 8117.
It now listens on port 8114.
Do not point clients at port 8000: that is the GPU metrics exporter.
`chatroom_post_message` no longer accepts `thread_title`.
The old `magickit-server` command was removed.
