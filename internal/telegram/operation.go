package telegram

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"strings"
	"time"
)

const OpPrefix = "TUP_OP:"

type OpType string

const (
	OpCP       OpType = "CP"
	OpMKDIR    OpType = "MKDIR"
	OpRM       OpType = "RM"
	OpMV       OpType = "MV"
	OpSNAPSHOT OpType = "SNAPSHOT"
	OpRESOLVE  OpType = "RESOLVE"
	OpFORMAT   OpType = "FORMAT"
)

type Operation struct {
	Version    int    `json:"v"`
	Hash       string `json:"hash"`
	PrevHash   string `json:"prev_hash"`
	Op         OpType `json:"op"`
	Path       string `json:"path"`
	TargetPath string `json:"target_path,omitempty"`
	Size       int64  `json:"size,omitempty"`
	Sha256     string `json:"sha256,omitempty"`
	MessageID  int    `json:"msg_id,omitempty"`
	Timestamp  int64  `json:"ts"`
}

// ComputeHash generates a deterministic SHA-256 hash for the operation commit
func (op *Operation) ComputeHash() string {
	raw := fmt.Sprintf("%s|%s|%s|%s|%d|%s|%d|%d",
		op.PrevHash, op.Op, op.Path, op.TargetPath, op.Size, op.Sha256, op.MessageID, op.Timestamp)
	h := sha256.Sum256([]byte(raw))
	return hex.EncodeToString(h[:])
}

// Encode converts the Operation to a TUP_OP string payload suitable for Telegram captions/text
func (op *Operation) Encode() (string, error) {
	if op.Version == 0 {
		op.Version = 1
	}
	if op.Timestamp == 0 {
		op.Timestamp = time.Now().Unix()
	}
	if op.Hash == "" {
		op.Hash = op.ComputeHash()
	}
	data, err := json.Marshal(op)
	if err != nil {
		return "", fmt.Errorf("failed to marshal operation payload: %w", err)
	}
	return OpPrefix + string(data), nil
}

// DecodeOperation parses a TUP_OP payload string from a Telegram message or caption
func DecodeOperation(text string) (*Operation, error) {
	idx := strings.Index(text, OpPrefix)
	if idx == -1 {
		return nil, fmt.Errorf("no TUP_OP payload found in text")
	}

	jsonStr := text[idx+len(OpPrefix):]
	// If there are trailing lines, take just the JSON object
	if endIdx := strings.Index(jsonStr, "\n"); endIdx != -1 {
		jsonStr = jsonStr[:endIdx]
	}
	jsonStr = strings.TrimSpace(jsonStr)

	var op Operation
	if err := json.Unmarshal([]byte(jsonStr), &op); err != nil {
		return nil, fmt.Errorf("failed to unmarshal operation payload: %w", err)
	}
	return &op, nil
}
