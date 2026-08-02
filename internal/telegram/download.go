package telegram

import (
	"context"
	"fmt"
	"io"
	"strconv"

	"github.com/gotd/td/telegram/downloader"
	"github.com/gotd/td/tg"
)

// DownloadFileMTProto downloads a file from Telegram using its Message ID and streams it to output.
func DownloadFileMTProto(ctx context.Context, chatIDStr string, messageID int, output io.Writer) error {
	return Run(ctx, func(ctx context.Context) error {
		api := Client.API()

		peer, err := resolvePeer(ctx, api, chatIDStr)
		if err != nil {
			return fmt.Errorf("peer resolution failed: %w", err)
		}

		id, _ := strconv.ParseInt(chatIDStr, 10, 64)
		isChannel := id <= -1000000000000

		var messages []tg.MessageClass
		if isChannel {
			channelPeer, ok := peer.(*tg.InputPeerChannel)
			if !ok {
				return fmt.Errorf("expected channel peer")
			}
			res, err := api.ChannelsGetMessages(ctx, &tg.ChannelsGetMessagesRequest{
				Channel: &tg.InputChannel{
					ChannelID:  channelPeer.ChannelID,
					AccessHash: channelPeer.AccessHash,
				},
				ID: []tg.InputMessageClass{&tg.InputMessageID{ID: messageID}},
			})
			if err != nil {
				return fmt.Errorf("failed to fetch message: %w", err)
			}
			switch m := res.(type) {
			case *tg.MessagesMessages:
				messages = m.Messages
			case *tg.MessagesMessagesSlice:
				messages = m.Messages
			case *tg.MessagesChannelMessages:
				messages = m.Messages
			}
		} else {
			res, err := api.MessagesGetMessages(ctx, []tg.InputMessageClass{
				&tg.InputMessageID{ID: messageID},
			})
			if err != nil {
				return fmt.Errorf("failed to fetch message: %w", err)
			}
			switch m := res.(type) {
			case *tg.MessagesMessages:
				messages = m.Messages
			case *tg.MessagesMessagesSlice:
				messages = m.Messages
			case *tg.MessagesChannelMessages:
				messages = m.Messages
			}
		}

		if len(messages) == 0 {
			return fmt.Errorf("message %d not found in Telegram", messageID)
		}

		msg, ok := messages[0].(*tg.Message)
		if !ok {
			return fmt.Errorf("message %d is not a standard message", messageID)
		}

		mediaDoc, ok := msg.Media.(*tg.MessageMediaDocument)
		if !ok || mediaDoc.Document == nil {
			return fmt.Errorf("message %d does not contain a document", messageID)
		}

		doc, ok := mediaDoc.Document.AsNotEmpty()
		if !ok {
			return fmt.Errorf("document in message %d is empty", messageID)
		}

		location := &tg.InputDocumentFileLocation{
			ID:            doc.ID,
			AccessHash:    doc.AccessHash,
			FileReference: doc.FileReference,
		}

		d := downloader.NewDownloader()
		_, err = d.Download(api, location).Stream(ctx, output)
		if err != nil {
			return fmt.Errorf("download failed: %w", err)
		}

		return nil
	})
}
