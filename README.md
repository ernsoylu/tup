# tup

`tup` treats Telegram chats and channels like a POSIX-compatible Virtual File System (VFS). With `tup`, you can use standard terminal commands (`cp`, `mv`, `rm`, `ls`) to manage remote files with native 2GB MTProto streaming bandwidth.

## Features

- **POSIX Interface**: Use `tup cp`, `tup ls`, `tup rm`, `tup mkdir` exactly as you would natively.
- **Native 2GB Support**: Utilizes the `gotd/td` MTProto client to unlock Telegram's native 2GB file upload limits.
- **VFS Backups**: Safely backup your drive's index into a JSON file embedded right in the chat.
- **Instant Restore**: Connect to an existing drive on a new device, and it will auto-restore the VFS from the chat history.
- **AI Ready**: Run `tup ai` to generate instructions that teach LLMs (Cursor, Claude, Copilot, Antigravity) how to interact with your cloud files natively.

## Installation

Install via curl:

```bash
curl -fsSL https://raw.githubusercontent.com/ernsoylu/tup/main/install.sh | sh
```

Or download the binary directly from [Releases](https://github.com/ernsoylu/tup/releases).

## Getting Started

### 1. Login

Run the interactive setup to configure your Telegram API credentials:
```bash
tup login
```
*Note: We highly recommend MTProto (2GB) mode. You will need your `API_ID` and `API_HASH` from [my.telegram.org](https://my.telegram.org).*

### 2. Register a Drive

Register any Telegram Chat ID as an alias:
```bash
tup drive add work <chat_id>
```
*(If a backup exists in this chat, `tup` will automatically restore your virtual file system cache!)*

### 3. Use POSIX Commands

Now, just use standard syntax:
```bash
# Upload a file to Telegram
tup cp ./local-report.pdf work:/docs/report.pdf

# List remote files
tup ls work:/docs

# Download a file
tup cp work:/docs/report.pdf ./downloaded.pdf

# Delete a remote file
tup rm work:/docs/report.pdf
```

## Updates

`tup` comes with a built-in self-updater. Just run:
```bash
tup update
```
It will automatically fetch and install the latest binary from GitHub!

## Development

Built natively in Go using `cobra` and `gotd/td`.

```bash
git clone https://github.com/ernsoylu/tup
cd tup
go build ./cmd/tup
```
