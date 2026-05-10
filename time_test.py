import time
import combine_live

start = time.time()
trades = combine_live.get_combined_exit_trades()
mid = time.time()
trades2 = combine_live.get_combined_exit_trades()
end = time.time()

print(f"First run (miss): {mid - start:.4f} sec")
print(f"Second run (hit): {end - mid:.4f} sec")
