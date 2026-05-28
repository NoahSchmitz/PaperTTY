#!/bin/bash

# 1. Get total terminal rows using tput
total_rows=$(tput lines)

# 2. Save current TTY settings and set to raw mode (to intercept the answer)
exec < /dev/tty
old_stty=$(stty -g)
stty raw -echo min 0

# 3. Send the Device Status Report request to the TTY
echo -en "\033[6n" > /dev/tty

# 4. Read the response until we hit the 'R' character
IFS=';' read -r -d R -a pos

# 5. Restore original TTY settings immediately
stty $old_stty

# 6. Parse the row number (stripping the leading \033[ )
current_row=$(echo "${pos[0]}" | sed 's/.*\[//')

# 7. Compare
if [ "$current_row" -eq "$total_rows" ]; then
    echo -e "\n🚨 You are on the last line ($current_row). The next line will scroll the screen!"
else
    echo -e "\n✅ You are on line $current_row of $total_rows. No scroll yet."
fi
