package main

import (
	"fmt"
	"os"
	"strings"

	"github.com/spf13/cobra"

	"github.com/multica-ai/multica/server/internal/cli"
)

var p0Cmd = &cobra.Command{
	Use:   "p0",
	Short: "Create and ACK P0 external notifications",
}

var p0NotifyCmd = &cobra.Command{
	Use:   "notify <body>",
	Short: "Create a P0 notification that remains pending until ACKed",
	Args:  exactArgs(1),
	RunE:  runP0Notify,
}

var p0PendingCmd = &cobra.Command{
	Use:   "pending",
	Short: "List pending P0 ACKs",
	Args:  exactArgs(0),
	RunE:  runP0Pending,
}

var p0AckCmd = &cobra.Command{
	Use:   "ack <id>",
	Short: "ACK a P0 notification",
	Args:  exactArgs(1),
	RunE:  runP0Ack,
}

type p0Notification struct {
	ID            string  `json:"id"`
	WorkspaceID   string  `json:"workspace_id"`
	Body          string  `json:"body"`
	Source        string  `json:"source"`
	CreatedByType string  `json:"created_by_type"`
	CreatedByID   string  `json:"created_by_id,omitempty"`
	AckedByType   *string `json:"acked_by_type,omitempty"`
	AckedByID     *string `json:"acked_by_id,omitempty"`
	AckNote       *string `json:"ack_note,omitempty"`
	AckedAt       *string `json:"acked_at,omitempty"`
	CreatedAt     string  `json:"created_at"`
	UpdatedAt     string  `json:"updated_at"`
}

type p0PendingResponse struct {
	Items []p0Notification `json:"items"`
}

func init() {
	p0NotifyCmd.Flags().String("source", "manual", "Source label for this notification")
	p0NotifyCmd.Flags().String("output", "table", "Output format: table or json")
	p0PendingCmd.Flags().String("output", "table", "Output format: table or json")
	p0AckCmd.Flags().String("note", "", "Optional ACK note")
	p0AckCmd.Flags().String("output", "table", "Output format: table or json")

	p0Cmd.AddCommand(p0NotifyCmd)
	p0Cmd.AddCommand(p0PendingCmd)
	p0Cmd.AddCommand(p0AckCmd)
}

func runP0Notify(cmd *cobra.Command, args []string) error {
	client, err := newAPIClient(cmd)
	if err != nil {
		return err
	}
	workspaceID, err := requireWorkspaceID(cmd)
	if err != nil {
		return err
	}
	ctx, cancel := cli.APIContext(cmd.Context())
	defer cancel()

	body := strings.TrimSpace(args[0])
	source, _ := cmd.Flags().GetString("source")
	var result p0Notification
	if err := client.PostJSON(ctx, "/api/workspaces/"+workspaceID+"/p0/notifications", map[string]any{
		"body":   body,
		"source": source,
	}, &result); err != nil {
		return err
	}
	return printP0Notification(cmd, result)
}

func runP0Pending(cmd *cobra.Command, _ []string) error {
	client, err := newAPIClient(cmd)
	if err != nil {
		return err
	}
	workspaceID, err := requireWorkspaceID(cmd)
	if err != nil {
		return err
	}
	ctx, cancel := cli.APIContext(cmd.Context())
	defer cancel()

	var result p0PendingResponse
	if err := client.GetJSON(ctx, "/api/workspaces/"+workspaceID+"/p0/pending", &result); err != nil {
		return err
	}
	output, _ := cmd.Flags().GetString("output")
	if output == "json" {
		return cli.PrintJSON(os.Stdout, result)
	}
	rows := make([][]string, 0, len(result.Items))
	for _, item := range result.Items {
		rows = append(rows, []string{item.ID, item.CreatedAt, item.Source, item.Body})
	}
	cli.PrintTable(os.Stdout, []string{"ID", "CREATED", "SOURCE", "BODY"}, rows)
	return nil
}

func runP0Ack(cmd *cobra.Command, args []string) error {
	client, err := newAPIClient(cmd)
	if err != nil {
		return err
	}
	workspaceID, err := requireWorkspaceID(cmd)
	if err != nil {
		return err
	}
	ctx, cancel := cli.APIContext(cmd.Context())
	defer cancel()

	note, _ := cmd.Flags().GetString("note")
	var result p0Notification
	if err := client.PostJSON(ctx, "/api/workspaces/"+workspaceID+"/p0/notifications/"+args[0]+"/ack", map[string]any{
		"note": note,
	}, &result); err != nil {
		return err
	}
	return printP0Notification(cmd, result)
}

func printP0Notification(cmd *cobra.Command, item p0Notification) error {
	output, _ := cmd.Flags().GetString("output")
	if output == "json" {
		return cli.PrintJSON(os.Stdout, item)
	}
	status := "pending"
	ackedAt := ""
	if item.AckedAt != nil {
		status = "acked"
		ackedAt = *item.AckedAt
	}
	cli.PrintTable(os.Stdout, []string{"ID", "STATUS", "CREATED", "ACKED", "SOURCE", "BODY"}, [][]string{{
		item.ID, status, item.CreatedAt, ackedAt, item.Source, item.Body,
	}})
	if status == "pending" {
		fmt.Fprintf(os.Stderr, "Pending ACK id: %s\n", item.ID)
	}
	return nil
}
