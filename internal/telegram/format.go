package telegram

import (
	"context"
	"fmt"
	"strconv"
	"time"

	"github.com/gotd/td/tg"
	"github.com/pterm/pterm"
)

// FormatChat deletes every message in a chat. This is irreversible.
// It must be called from inside a Run() callback.
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

	for {
		// Fetch a batch of messages
		res, err := api.MessagesGetHistory(ctx, &tg.MessagesGetHistoryRequest{
			Peer:  peer,
			Limit: 100,
		})
		if err != nil {
			return fmt.Errorf("failed to fetch message history: %w", err)
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

		// Collect message IDs
		ids := make([]int, 0, len(messages))
		for _, msg := range messages {
			switch m := msg.(type) {
			case *tg.Message:
				ids = append(ids, m.ID)
			case *tg.MessageService:
				ids = append(ids, m.ID)
			}
		}

		if len(ids) == 0 {
			break
		}

		// Delete the batch
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
			return fmt.Errorf("failed to delete messages: %w", err)
		}

		totalDeleted += len(ids)
		pterm.Info.Printf("Deleted %d messages so far...\n", totalDeleted)

		// Small delay to avoid rate limiting
		time.Sleep(500 * time.Millisecond)
	}

	pterm.Success.Printf("Format complete! Deleted %d messages total.\n", totalDeleted)
	return nil
}
