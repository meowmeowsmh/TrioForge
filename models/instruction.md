before you do anything you can do something like putting your gguf file in here 

how to use command => 
llama-server -m "models\<mode_name>.gguf" --port 8080

this is to help out for people who don't know what is happening 

for vision models:
llama-server -m "models\<model>.gguf" --mmproj "<nmproject>" --port 8080
example: 
llama-server -m "models\Qwen3.5-9B-Uncensored-HauhauCS-Aggressive-Q4_K_M.gguf" --mmproj "models\mmproj-Qwen3.5-9B-Uncensored-HauhauCS-Aggressive-BF16.gguf" --port 8080