package telegram

import (
	"context"
	"fmt"
	"strconv"
	"time"

	"github.com/gotd/td/telegram"
	"github.com/gotd/td/tg"
)

// DeleteMessages removes Telegram messages by ID from the given chat.
// Missing/already-deleted messages are ignored by the API when possible.
func DeleteMessages(ctx context.Context, chatIDStr string, messageIDs []int) error {
	if len(messageIDs) == 0 {
		return nil
	}

	// Dedupe
	seen := make(map[int]struct{}, len(messageIDs))
	ids := make([]int, 0, len(messageIDs))
	for _, id := range messageIDs {
		if id == 0 {
			continue
		}
		if _, ok := seen[id]; ok {
			continue
		}
		seen[id] = struct{}{}
		ids = append(ids, id)
	}
	if len(ids) == 0 {
		return nil
	}

	return Run(ctx, func(ctx context.Context) error {
		api := Client.API()
		peer, err := resolvePeer(ctx, api, chatIDStr)
		if err != nil {
			return fmt.Errorf("peer resolution failed: %w", err)
		}

		chatID, _ := strconv.ParseInt(chatIDStr, 10, 64)
		isChannel := chatID <= -1000000000000

		// Batch in chunks of 100
		for start := 0; start < len(ids); start += 100 {
			end := start + 100
			if end > len(ids) {
				end = len(ids)
			}
			batch := ids[start:end]

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
						ID: batch,
					})
				} else {
					_, err = api.MessagesDeleteMessages(ctx, &tg.MessagesDeleteMessagesRequest{
						Revoke: true,
						ID:     batch,
					})
				}
				if err != nil {
					if d, ok := telegram.AsFloodWait(err); ok {
						time.Sleep(d + time.Second)
						continue
					}
					return fmt.Errorf("failed to delete messages: %w", err)
				}
				break
			}
		}
		return nil
	})
}
