import argparse
import sys
from pathlib import Path

import librosa
import numpy as np
import torch
from inference.encoder import inference as encoder
from inference.encoder.params_model import (
    model_embedding_size as speaker_embedding_size,
)
from inference.synthesizer.inference import Synthesizer
from inference.utils.argutils import print_args
from inference.vocoder import inference as vocoder

if __name__ == "__main__":
    ## Info & args
    parser = argparse.ArgumentParser(
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    parser.add_argument(
        "-e",
        "--enc_model_fpath",
        type=Path,
        default="inference/encoder/saved_models/pretrained.pt",
        help="Path to a saved encoder",
    )
    parser.add_argument(
        "-s",
        "--syn_model_dir",
        type=Path,
        default="inference/synthesizer/saved_models/logs-pretrained/",
        help="Directory containing the synthesizer model",
    )
    parser.add_argument(
        "-v",
        "--voc_model_fpath",
        type=Path,
        default="inference/vocoder/saved_models/pretrained/pretrained.pt",
        help="Path to a saved vocoder",
    )
    parser.add_argument(
        "--low_mem",
        action="store_true",
        help="If True, the memory used by the synthesizer will be freed after each use. Adds large "
        "overhead but allows to save some GPU memory for lower-end GPUs.",
    )
    parser.add_argument(
        "--no_sound", action="store_true", help="If True, audio won't be played."
    )
    args = parser.parse_args()
    print_args(args, parser)
    if not args.no_sound:
        import sounddevice as sd

    ## Print some environment information (for debugging purposes)
    print("Running a test of your configuration...\n")
    if not torch.cuda.is_available():
        print(
            "Your PyTorch installation is not configured to use CUDA. If you have a GPU ready "
            "for deep learning, ensure that the drivers are properly installed, and that your "
            "CUDA version matches your PyTorch installation. CPU-only inference is currently "
            "not supported.",
            file=sys.stderr,
        )
        sys.exit()
    device_id = torch.cuda.current_device()
    gpu_properties = torch.cuda.get_device_properties(device_id)
    print(
        "Found %d GPUs available. Using GPU %d (%s) of compute capability %d.%d with "
        "%.1fGb total memory.\n"
        % (
            torch.cuda.device_count(),
            device_id,
            gpu_properties.name,
            gpu_properties.major,
            gpu_properties.minor,
            gpu_properties.total_memory / 1e9,
        )
    )

    ## Load the models one by one.
    print("Preparing the encoder, the synthesizer and the vocoder...")
    encoder.load_model(args.enc_model_fpath)
    synthesizer = Synthesizer(
        args.syn_model_dir.joinpath("taco_pretrained"), low_mem=args.low_mem
    )
    vocoder.load_model(args.voc_model_fpath)

    ## Run a test
    print("Testing your configuration with small inputs.")
    # Forward an audio waveform of zeroes that lasts 1 second. Notice how we can get the encoder's
    # sampling rate, which may differ.
    # If you're unfamiliar with digital audio, know that it is encoded as an array of floats
    # (or sometimes integers, but mostly floats in this projects) ranging from -1 to 1.
    # The sampling rate is the number of values (samples) recorded per second, it is set to
    # 16000 for the encoder. Creating an array of length <sampling_rate> will always correspond
    # to an audio of 1 second.
    print("\tTesting the encoder...")
    encoder.embed_utterance(np.zeros(encoder.sampling_rate))

    # Create a dummy embedding. You would normally use the embedding that encoder.embed_utterance
    # returns, but here we're going to make one ourselves just for the sake of showing that it's
    # possible.
    embed = np.random.rand(speaker_embedding_size)
    # Embeddings are L2-normalized (this isn't important here, but if you want to make your own
    # embeddings it will be).
    embed /= np.linalg.norm(embed)
    # The synthesizer can handle multiple inputs with batching. Let's create another embedding to
    # illustrate that
    embeds = [embed, np.zeros(speaker_embedding_size)]
    texts = ["test 1", "test 2"]
    print("\tTesting the synthesizer... (loading the model will output a lot of text)")
    mels = synthesizer.synthesize_spectrograms(texts, embeds)

    # The vocoder synthesizes one waveform at a time, but it's more efficient for long ones. We
    # can concatenate the mel spectrograms to a single one.
    mel = np.concatenate(mels, axis=1)
    # The vocoder can take a callback function to display the generation. More on that later. For
    # now we'll simply hide it like this:
    no_action = lambda *args: None
    print("\tTesting the vocoder...")
    # For the sake of making this test short, we'll pass a short target length. The target length
    # is the length of the wav segments that are processed in parallel. E.g. for audio sampled
    # at 16000 Hertz, a target length of 8000 means that the target audio will be cut in chunks of
    # 0.5 seconds which will all be generated together. The parameters here are absurdly short, and
    # that has a detrimental effect on the quality of the audio. The default parameters are
    # recommended in general.
    vocoder.infer_waveform(mel, target=200, overlap=50, progress_callback=no_action)

    print("All test passed! You can now synthesize speech.\n\n")

    ## Interactive speech generation
    print(
        "This is a GUI-less example of interface to SV2TTS. The purpose of this script is to "
        "show how you can interface this project easily with your own. See the source code for "
        "an explanation of what is happening.\n"
    )

    print("Interactive generation loop")
    num_generated = 0
    while True:
        try:
            # Get the reference audio filepath
            message = (
                "Reference voice: enter an audio filepath of a voice to be cloned (mp3, "
                "wav, m4a, flac, ...):\n"
            )
            in_fpath = Path(input(message).replace('"', "").replace("'", ""))

            ## Computing the embedding
            # First, we load the wav using the function that the speaker encoder provides. This is
            # important: there is preprocessing that must be applied.

            # The following two methods are equivalent:
            # - Directly load from the filepath:
            preprocessed_wav = encoder.preprocess_wav(in_fpath)
            # - If the wav is already loaded:
            original_wav, sampling_rate = librosa.load(in_fpath)
            preprocessed_wav = encoder.preprocess_wav(original_wav, sampling_rate)
            print("Loaded file succesfully")

            # Then we derive the embedding. There are many functions and parameters that the
            # speaker encoder interfaces. These are mostly for in-depth research. You will typically
            # only use this function (with its default parameters):
            embed = encoder.embed_utterance(preprocessed_wav)
            print("Created the embedding")

            ## Generating the spectrogram
            text = input("Write a sentence (+-20 words) to be synthesized:\n")

            # The synthesizer works in batch, so you need to put your data in a list or numpy array
            texts = [text]
            embeds = [embed]
            # If you know what the attention layer alignments are, you can retrieve them here by
            # passing return_alignments=True
            specs = synthesizer.synthesize_spectrograms(texts, embeds)
            spec = specs[0]
            print("Created the mel spectrogram")

            ## Generating the waveform
            print("Synthesizing the waveform:")
            # Synthesizing the waveform is fairly straightforward. Remember that the longer the
            # spectrogram, the more time-efficient the vocoder.
            generated_wav = vocoder.infer_waveform(spec)

            ## Post-generation
            # There's a bug with sounddevice that makes the audio cut one second earlier, so we
            # pad it.
            generated_wav = np.pad(
                generated_wav, (0, synthesizer.sample_rate), mode="constant"
            )

            # Play the audio (non-blocking)
            if not args.no_sound:
                sd.stop()
                sd.play(generated_wav, synthesizer.sample_rate)

            # Save it on the disk
            fpath = "demo_output_%02d.wav" % num_generated
            print(generated_wav.dtype)
            librosa.output.write_wav(
                fpath, generated_wav.astype(np.float32), synthesizer.sample_rate
            )
            num_generated += 1
            print("\nSaved output as %s\n\n" % fpath)

        except Exception as e:
            print("Caught exception: %s" % repr(e))
            print("Restarting\n")
