package telegram

import (
	"context"
	crand "crypto/rand"
	"encoding/binary"
	"fmt"
	"strconv"
	"time"

	"github.com/gotd/td/telegram"
	"github.com/gotd/td/tg"
	"github.com/pterm/pterm"
)

// FormatChat deletes every message in a chat. This is irreversible.
// It automatically handles FLOOD_WAIT rate limits and prevents infinite loops on non-deletable system messages.
func FormatChat(ctx context.Context, chatIDStr string) error {
	api := Client.API()

	pterm.Info.Printf("Resolving peer %s...\n", chatIDStr)
	peer, err := resolvePeer(ctx, api, chatIDStr)
	if err != nil {
		return fmt.Errorf("peer resolution failed: %w", err)
	}

	id, _ := strconv.ParseInt(chatIDStr, 10, 64)
	isChannel := id <= -1000000000000

	totalDeleted := 0
	attemptedIDs := make(map[int]bool)

	for {
		// Fetch a batch of messages with FLOOD_WAIT retry
		var res tg.MessagesMessagesClass
		for {
			var err error
			res, err = api.MessagesGetHistory(ctx, &tg.MessagesGetHistoryRequest{
				Peer:  peer,
				Limit: 100,
			})
			if err != nil {
				if d, ok := telegram.AsFloodWait(err); ok {
					waitDur := d + time.Second
					pterm.Warning.Printf("Rate limit hit (FLOOD_WAIT). Waiting %v before retrying...\n", waitDur)
					time.Sleep(waitDur)
					continue
				}
				return fmt.Errorf("failed to fetch message history: %w", err)
			}
			break
		}

		var messages []tg.MessageClass
		switch m := res.(type) {
		case *tg.MessagesMessages:
			messages = m.Messages
		case *tg.MessagesMessagesSlice:
			messages = m.Messages
		case *tg.MessagesChannelMessages:
			messages = m.Messages
		}

		if len(messages) == 0 {
			break
		}

		// Collect message IDs and check if they've already been attempted
		ids := make([]int, 0, len(messages))
		newIDsFound := false

		for _, msg := range messages {
			var msgID int
			switch m := msg.(type) {
			case *tg.Message:
				msgID = m.ID
			case *tg.MessageService:
				msgID = m.ID
			}

			if msgID != 0 {
				if !attemptedIDs[msgID] {
					newIDsFound = true
					attemptedIDs[msgID] = true
				}
				ids = append(ids, msgID)
			}
		}

		// If no new deletable messages were found (i.e. all remaining messages are system creation messages that Telegram won't delete), stop
		if !newIDsFound || len(ids) == 0 {
			break
		}

		// Delete the batch with FLOOD_WAIT retry
		for {
			var err error
			if isChannel {
				channelPeer, ok := peer.(*tg.InputPeerChannel)
				if !ok {
					return fmt.Errorf("expected channel peer for channel ID")
				}
				_, err = api.ChannelsDeleteMessages(ctx, &tg.ChannelsDeleteMessagesRequest{
					Channel: &tg.InputChannel{
						ChannelID:  channelPeer.ChannelID,
						AccessHash: channelPeer.AccessHash,
					},
					ID: ids,
				})
			} else {
				_, err = api.MessagesDeleteMessages(ctx, &tg.MessagesDeleteMessagesRequest{
					Revoke: true,
					ID:     ids,
				})
			}

			if err != nil {
				if d, ok := telegram.AsFloodWait(err); ok {
					waitDur := d + time.Second
					pterm.Warning.Printf("Rate limit hit (FLOOD_WAIT). Waiting %v before retrying...\n", waitDur)
					time.Sleep(waitDur)
					continue
				}
				return fmt.Errorf("failed to delete messages: %w", err)
			}
			break
		}

		totalDeleted += len(ids)
		pterm.Info.Printf("Deleted %d messages so far...\n", totalDeleted)

		// 1 second delay between batches to be respectful of Telegram rate limits
		time.Sleep(1 * time.Second)
	}

	pterm.Success.Printf("Format complete! Deleted %d messages total.\n", totalDeleted)

	// Send OpFORMAT marker payload so other devices auto-reset their local VFS database on sync
	op := &Operation{
		Op:   OpFORMAT,
		Path: "/",
	}
	if payload, err := op.Encode(); err == nil {
		var randBuf [8]byte
		_, _ = crand.Read(randBuf[:])
		randomID := int64(binary.LittleEndian.Uint64(randBuf[:]))
		_, _ = api.MessagesSendMessage(ctx, &tg.MessagesSendMessageRequest{
			Peer:     peer,
			Message:  payload,
			RandomID: randomID,
		})
	}

	return nil
}
