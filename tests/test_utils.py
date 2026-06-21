from export_media import human_mb, human_gb, truncate_filename, is_generic_name

def test_human_mb():
    assert human_mb(1024 * 1024) == 1.0
    assert human_mb(1024 * 1024 * 5) == 5.0

def test_human_gb():
    assert human_gb(1024 * 1024 * 1024) == 1.0
    assert human_gb(1024 * 1024 * 1024 * 2) == 2.0

def test_truncate_filename():
    # If the prefix + filename fits within the limit, return filename as-is:
    assert truncate_filename("myfile.txt", "msg_123_") == "myfile.txt"
    # If it exceeds the limit, it should truncate the middle:
    assert truncate_filename("a" * 30 + ".txt", "msg_123_", 20) == "aaaa…aaa.txt"
    
def test_is_generic_name():
    # Static fallback and regex generic names
    assert is_generic_name("video.mp4") is True
    assert is_generic_name("photo.jpg") is True
    assert is_generic_name("12345.mp4") is True
    assert is_generic_name("My Custom Photo.jpg") is False

